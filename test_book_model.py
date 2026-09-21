import unittest
from book_model import parse_json, resource_name, validate_parts, build_chapters
from lxml import html


class BookModelTests(unittest.TestCase):
    def manifest(self):
        return {'Meta':{'Title':'Книга','Authors':[{'First':'А','Last':'Б'}]},
                'Parts':[{'s':0,'e':1,'url':'000.js'}],
                'Body':[{'s':0,'e':1,'c':[]}]}

    def test_literal_parser_rejects_executable_javascript(self):
        self.assertEqual(parse_json('{x:"текст",}'),{'x':'текст'})
        with self.assertRaises(Exception):
            parse_json('(function(){return {x:1}})()')

    def test_missing_or_overlapping_parts_do_not_pass(self):
        toc = self.manifest()
        with self.assertRaises(ValueError):
            validate_parts(toc,[[{'t':'p'}]])
        toc['Parts'] = [{'s':1,'e':2,'url':'000.js'}]
        with self.assertRaises(ValueError):
            validate_parts(toc,[[{'t':'p'},{'t':'p'}]])

    def test_preserves_text_image_and_footnote(self):
        toc = self.manifest()
        blocks = [{'t':'p','xp':[1,1],'c':['Те\u00adкст <важно>',
            {'t':'note','xp':[1,1,2],'c':['[1]'],'f':{'t':'footnote','xp':[1,1,2,1],
                'c':[{'t':'p','c':['Полная сноска & пояснение']}]}}]},
            {'t':'img','xp':[1,2],'s':'img123.jpg','w':100,'h':200}]
        title,author,chapters,images,notes = build_chapters(toc,validate_parts(toc,[blocks]))
        tree = html.fragment_fromstring(chapters[0]['html'],create_parent='div')
        self.assertIn('Текст <важно>',tree.text_content())
        self.assertIn('Полная сноска & пояснение',tree.text_content())
        self.assertEqual(images,{'img123.jpg'})
        self.assertEqual(notes,1)
        href = tree.xpath('//sup/a/@href')[0]
        self.assertEqual(len(tree.xpath(f'//*[@id="{href[1:]}"]')),1)

    def test_rejects_network_and_traversal_resource_names(self):
        for value in ('../token','https://example.com/a','//evil.test/a','C:\\secret','%2e%2e/secret'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resource_name(value)


if __name__ == '__main__':
    unittest.main()
