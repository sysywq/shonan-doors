import unittest
from urllib.parse import urljoin
import build
import post_to_facebook as fb

class SharedHeroTests(unittest.TestCase):
    def item(self, **kw):
        return dict(id=272,slug='hiratsuka-event-0272',title='公演',dek='紹介',cat='e',
                    area='平塚',scene='festival',date='2026-11-07',**kw)
    def test_category_photo_replaces_old_scene_illustration(self):
        a=self.item()
        expected=urljoin(build.SITE_DOMAIN,build.resolve_article_hero_image(a)[0])
        self.assertEqual(build.build_ogp_image_url(a),expected)
        head=build.render_head_seo(a,fb.article_url(a))
        self.assertIn('og:image" content="'+expected+'"',head)
        self.assertIn('twitter:image" content="'+expected+'"',head)
        self.assertNotIn('/assets/ogp/festival.png',head)
        self.assertNotIn('og:image:width',head)
    def test_article_specific_and_absolute_photos_are_preserved(self):
        for source in ['/assets/images/articles/272.jpg','https://images.example/photo.jpg']:
            a=self.item(heroImage=source)
            self.assertEqual(build.build_ogp_image_url(a),urljoin(build.SITE_DOMAIN,source))
    def test_stale_or_missing_or_conflicting_share_image_blocks_post(self):
        url=fb.article_url(self.item())
        src='/assets/images/category-photos/event.webp'
        absolute=build.SITE_DOMAIN+src
        good=f'<meta property="og:image" content="{absolute}"><div class="modal-art"><img src="{src}"></div>'
        self.assertTrue(fb.preview_matches_hero(good,url,absolute))
        self.assertFalse(fb.preview_matches_hero(good,url,'https://different.example/photo.jpg'))
        old=good.replace('content="'+absolute+'"','content="https://www.shonandoors.com/assets/ogp/festival.png"')
        self.assertFalse(fb.preview_matches_hero(old,url))
        self.assertFalse(fb.preview_matches_hero('<div class="modal-art"><img src="'+src+'"></div>',url))
        self.assertFalse(fb.preview_matches_hero('<meta property="og:image" content="'+absolute+'">'+good,url))
