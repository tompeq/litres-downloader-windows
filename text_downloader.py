"""Download an accessible Litres text edition through its regular reader session."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import re
import tempfile
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from book_model import parse_json, resource_name, validate_parts, build_chapters
from ebook_export import write_epub


PRINT_CSS = '''
@page { size: A4; margin: 18mm 18mm 18mm; }
html { color: #111; background: white; }
body { margin: 0; font: 11pt/1.45 Georgia, 'Times New Roman', serif; }
h1,h2,h3 { font-family: Arial, sans-serif; line-height: 1.23; break-after: avoid; }
h1 { font-size: 22pt; margin: 0 0 1em; }
h2 { font-size: 16pt; margin: 1.5em 0 .65em; }
h3 { font-size: 12pt; margin: 1.2em 0 .5em; }
p { margin: 0 0 .6em; orphans: 3; widows: 3; }
.chapter { break-before: page; }
.chapter:first-of-type { break-before: auto; }
img { display:block; max-width:100%; max-height:230mm; width:auto; height:auto; margin:.7em auto; object-fit:contain; break-inside:avoid; }
blockquote,.epigraph { margin:1em 1.3em; }
.epigraph { font-style:italic; }
.subscription { text-align:right; }
.stanza { margin:1em 0; break-inside:avoid; }
.stanza p { margin:0; }
.footnotes { font-size:9pt; margin-top:2em; border-top:1px solid #bbb; }
.footnotes h2 { font-size:12pt; }
.footnotes aside { margin:.8em 0; }
.footnotes aside h2 { font-size:10pt; }
sup { font-size:75%; }
a { color:inherit; text-decoration:none; }
table { border-collapse:collapse; width:100%; }
td,th { border:1px solid #aaa; padding:.3em; }
'''


def checked_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.litres.ru', 'litres.ru') or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Only HTTPS URLs on litres.ru are supported')
    return url


def create_driver(headful=False):
    options = webdriver.ChromeOptions()
    options.add_argument('--window-size=1440,1080')
    if not headful:
        options.add_argument('--headless=new')
    # Chrome uses its temporary profile; no persistent cookies or saved passwords.
    options.add_experimental_option('prefs', {'credentials_enable_service':False,'profile.password_manager_enabled':False})
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(45)
    return driver


def login(driver, email: str, password: str):
    wait = WebDriverWait(driver, 40)
    driver.get('https://www.litres.ru/auth/login/')
    wait.until(EC.visibility_of_element_located((By.NAME,'email'))).send_keys(email)
    driver.find_element(By.XPATH,"//button[contains(.,'Продолжить')]").click()
    wait.until(EC.visibility_of_element_located((By.NAME,'pwd'))).send_keys(password)
    driver.find_element(By.XPATH,"//button[contains(.,'Войти')]").click()
    try:
        wait.until(lambda d: '/auth/' not in d.current_url and '/pages/login' not in d.current_url)
    except Exception as exc:
        raise RuntimeError('Вход не завершён. Проверьте пароль; для CAPTCHA/кода запустите с --headful.') from exc
    print('Вход выполнен.', flush=True)


def find_reader(driver, url: str) -> str:
    driver.get(checked_url(url))
    wait = WebDriverWait(driver, 40)
    try:
        element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/static/reader/text/"]')))
    except Exception as exc:
        raise RuntimeError('Ссылка на текстовую читалку не найдена. Проверьте доступ к книге в аккаунте.') from exc
    reader = checked_url(element.get_attribute('href'))
    base = parse_qs(urlsplit(reader).query).get('baseurl', [''])[0]
    if not base.startswith(('/download_book/', '/download_book_subscr/')):
        raise RuntimeError('Книга доступна только как фрагмент либо формат адреса читалки изменился.')
    return reader


def session_from_browser(driver):
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=.8, status_forcelist=[429,500,502,503,504], allowed_methods=['GET'], respect_retry_after_header=False)
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers['User-Agent'] = driver.execute_script('return navigator.userAgent')
    for cookie in driver.get_cookies():
        if cookie['domain'].lstrip('.') in ('litres.ru','www.litres.ru'):
            session.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie['path'],secure=cookie.get('secure',True))
    return session


def download_resource(session, url: str, limit=30_000_000):
    # Do not follow unexpected redirects or send account information elsewhere.
    with session.get(checked_url(url), timeout=(15,40), stream=True, allow_redirects=False) as response:
        if response.status_code in (401,403):
            raise RuntimeError('Сервер не разрешил доступ к содержимому книги.')
        if 300 <= response.status_code < 400:
            raise RuntimeError('Сервер перенаправил запрос: проверьте доступ к книге.')
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError('Reader resource exceeds the download size limit')
        return bytes(data)


def fetch_book(driver, reader_url: str, cache: Path | None = None):
    checked_url(reader_url)
    base = parse_qs(urlsplit(reader_url).query).get('baseurl',[''])[0]
    if not base.startswith(('/download_book/','/download_book_subscr/')) or '..' in base or '%' in base:
        raise ValueError('Not a full text reader URL')
    base_url = checked_url(urljoin('https://www.litres.ru', base.rstrip('/') + '/json/'))
    with session_from_browser(driver) as session:
        toc_raw = download_resource(session, urljoin(base_url,'toc.js'),12_000_000)
        toc = parse_json(toc_raw.decode('utf-8'))
        if not isinstance(toc,dict) or not toc.get('Parts') or len(toc['Parts']) > 2000:
            raise ValueError('Invalid reader manifest')
        chunks = []
        for index, part in enumerate(toc['Parts'],1):
            name = resource_name(part['url'])
            data = download_resource(session,urljoin(base_url,name),12_000_000)
            chunks.append(parse_json(data.decode('utf-8')))
            print(f'Текст: {index}/{len(toc["Parts"])}',flush=True)
        blocks = validate_parts(toc,chunks)
        title, author, chapters, images, notes = build_chapters(toc,blocks)
        assets = {}
        for index,name in enumerate(sorted(images),1):
            data = download_resource(session,urljoin(base_url,resource_name(name)))
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            assets['images/' + name] = data
            if index % 20 == 0 or index == len(images):
                print(f'Иллюстрации: {index}/{len(images)}',flush=True)
        report = {'title':title,'author':author,'parts':len(chunks),'blocks':len(blocks),
                  'chapters':len(chapters),'images':len(assets),'footnotes':notes,
                  'source_art_id':str(toc['Meta'].get('ArtID','')), 'complete':True,
                  'image_sha256':{name:hashlib.sha256(data).hexdigest() for name,data in assets.items()}}
        result = {'title':title,'author':author,'chapters':chapters,'assets':assets,'report':report}
        if cache:
            cache.mkdir(parents=True,exist_ok=True)
            (cache/'book.json').write_text(json.dumps({k:v for k,v in result.items() if k != 'assets'},ensure_ascii=False),encoding='utf-8')
            for name,data in assets.items():
                target = cache / name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(data)
        return result


def load_cache(cache: Path):
    book = json.loads((cache/'book.json').read_text(encoding='utf-8'))
    assets = {}
    for name,digest in book['report']['image_sha256'].items():
        data = (cache / resource_name(name)).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Cached image checksum mismatch')
        assets[name] = data
    book['assets'] = assets
    return book


def write_pdf(driver, destination: Path, book: dict):
    with tempfile.TemporaryDirectory(prefix='litres-print-') as directory:
        root = Path(directory)
        for name,data in book['assets'].items():
            target = root / resource_name(name)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(data)
        body = ''.join('<section class="chapter">' + chapter['html'] + '</section>' for chapter in book['chapters'])
        page = '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>' + escape(book['title']) + '</title><style>' + PRINT_CSS + '</style></head><body>' + body + '</body></html>'
        html_path = root/'book.html'
        html_path.write_text(page,encoding='utf-8')
        driver.get(html_path.as_uri())
        WebDriverWait(driver,40).until(lambda d:d.execute_script('return Array.from(document.images).every(i=>i.complete && i.naturalWidth>0)'))
        driver.execute_async_script('document.fonts.ready.then(()=>arguments[0](true))')
        pdf = driver.execute_cdp_cmd('Page.printToPDF', {
            'printBackground':True,'preferCSSPageSize':True,'displayHeaderFooter':True,
            'headerTemplate':'<span></span>',
            'footerTemplate':'<div style="width:100%;text-align:center;font-size:8px;color:#666"><span class="pageNumber"></span> / <span class="totalPages"></span></div>',
            'generateDocumentOutline':True,
        })
        destination.write_bytes(base64.b64decode(pdf['data']))


def export_book(driver, book: dict, output: Path, stem: str, formats=('epub','pdf')):
    output.mkdir(parents=True,exist_ok=True)
    if not re.fullmatch(r'[\w .-]+',stem) or stem.startswith('.') or stem.endswith(('.', ' ')):
        raise ValueError('Unsafe output name')
    targets = [output/(stem+'.'+extension) for extension in formats] + [output/(stem+'.verification.json')]
    if any(path.exists() for path in targets):
        raise FileExistsError('Выходной файл уже существует; выберите другое имя или каталог.')
    # Commit outputs only when every requested format was successfully built.
    with tempfile.TemporaryDirectory(prefix='.litres-',dir=output) as directory:
        staging = Path(directory)
        if 'epub' in formats:
            write_epub(staging/(stem+'.epub'),book['title'],book['author'],book['chapters'],book['assets'])
        if 'pdf' in formats:
            write_pdf(driver,staging/(stem+'.pdf'),book)
        (staging/(stem+'.verification.json')).write_text(json.dumps(book['report'],ensure_ascii=False,indent=2),encoding='utf-8')
        for path in staging.iterdir():
            path.replace(output/path.name)
    print('Готово: ' + str(output.resolve()),flush=True)


def main():
    parser = argparse.ArgumentParser(description='Сохранение доступной книги Литрес из текстовой читалки в EPUB/PDF')
    parser.add_argument('--url',help='Адрес карточки книги')
    parser.add_argument('--login',help='Почта или логин (пароль запрашивается скрыто)')
    parser.add_argument('--output',type=Path,default=Path('books'))
    parser.add_argument('--name',default='book')
    parser.add_argument('--format',choices=('epub','pdf','both'),default='both')
    parser.add_argument('--headful',action='store_true',help='Показать Chrome для ручного подтверждения входа')
    parser.add_argument('--cache',type=Path,help='Необязательно: сохранить текст и изображения для повторного экспорта, без сессии')
    parser.add_argument('--from-cache',type=Path,help='Повторный экспорт ранее сохранённой книги, без аккаунта')
    args = parser.parse_args()
    if not args.from_cache and not args.url:
        parser.error('--url is required unless --from-cache is supplied')
    formats = ('epub','pdf') if args.format == 'both' else (args.format,)
    driver = None
    try:
        if args.from_cache:
            book = load_cache(args.from_cache)
            if 'pdf' in formats:
                driver = create_driver(args.headful)
        else:
            checked_url(args.url)
            email = args.login or input('Логин: ')
            password = getpass.getpass('Пароль: ')
            driver = create_driver(args.headful)
            login(driver,email,password)
            password = None
            reader = find_reader(driver,args.url)
            driver.get(reader)
            book = fetch_book(driver,reader,args.cache)
        export_book(driver,book,args.output,args.name,formats)
    finally:
        if driver:
            driver.quit()


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt,Exception) as exc:
        # Never dump cookies, passwords or response bodies in error reports.
        print('Ошибка: ' + str(exc).split('Stacktrace:')[0])
        raise SystemExit(1)
