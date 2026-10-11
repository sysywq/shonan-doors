import unittest
import build


class EventStructuredDataTests(unittest.TestCase):
    def item(self, **extra):
        return dict(cat='e', area='逗子', title='公演', dek='解説', scene='marina',
                    eventStartDate='2026-10-18', sources=['https://official.example/event'], **extra)

    def test_missing_optional_fields_keeps_event_and_page(self):
        for extra in ({}, dict(eventPrice=None, eventOrganizer=None, eventPerformers=None)):
            item = self.item(**extra)
            data = build.build_structured_data(item, 'https://example.com/article')
            self.assertEqual(data['@type'], 'Event')
            for key in ('offers', 'organizer', 'performer'):
                self.assertNotIn(key, data)
            build.render_infobox(item)

    def test_no_source_no_metadata_and_no_inferred_free_admission(self):
        for price in ('', '不明', '-1', 'NaN', '1,000', {}, 0):
            item = self.item(eventMetadataSource='https://official.example/event',
                             eventPrice=price, eventTicketUrl='https://official.example/ticket')
            self.assertNotIn('offers', build.event_optional_data(item))
        item = self.item(eventPrice='0', eventTicketUrl='https://official.example/ticket')
        self.assertEqual(build.event_optional_data(item), {})

    def test_explicit_free_and_valid_people_and_groups(self):
        item = self.item(eventMetadataSource='https://official.example/event', eventPrice='0',
                         eventTicketUrl='https://official.example/ticket', eventOrganizer='主催者',
                         eventOrganizerUrl='javascript:alert(1)',
                         eventPerformers=[None, {'name': '演奏者', 'type': 'Person'},
                                          {'name': '楽団', 'type': 'PerformingGroup'},
                                          {'name': '出店者', 'type': 'Unknown'}])
        data = build.event_optional_data(item)
        self.assertEqual(data['offers']['price'], '0')
        self.assertNotIn('availability', data['offers'])
        self.assertNotIn('url', data['organizer'])
        self.assertEqual(len(data['performer']), 2)
        self.assertIn('主催者', build.render_infobox(item))
        self.assertIn('0円', build.render_infobox(item))

    def test_bad_ticket_url_only_omits_offer(self):
        item = self.item(eventMetadataSource='https://official.example/event', eventPrice='2000',
                         eventTicketUrl='javascript:alert(1)', eventOrganizer='主催者')
        data = build.event_optional_data(item)
        self.assertNotIn('offers', data)
        self.assertIn('organizer', data)
