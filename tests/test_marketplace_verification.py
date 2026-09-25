"""Offline regressions for seller identity versus website ownership."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database_lite as db
from discovery.website_verifier import verify, blocked_url, score_page
from discovery.email_discovery import crawl_site
from test_phase2_discovery import COMPANY, FakeFetcher, response

IDENTITY = '''ALFA d.o.o. SI12345678 Matična 7654321 Glavna ulica 12, 1000 Ljubljana
<script type="application/ld+json">{"@type":"Organization","name":"ALFA d.o.o.","taxID":"SI12345678"}</script>'''


class MarketplaceTests(unittest.TestCase):
    def test_original_mascus_evidence_cannot_establish_ownership(self):
        company = {**COMPANY, 'company_name': 'KOROŠAK, d.o.o.', 'tax_number': '34231749',
                   'registration_number': '2303442', 'address': 'Glavna ulica 2, 9231 Beltinci',
                   'municipality': '9231 Beltinci'}
        url = 'https://www.mascus.co.uk/korosak-d-o-o/2ebb6020,1,relevance,searchdealer.html'
        html = '''<title>KOROŠAK d.o.o. - Mascus Slovenia</title>
        KOROŠAK d.o.o. Glavna ulica 2, 9231 Beltinci SI34231749 Matična 2303442
        <script type="application/ld+json">{"@type":"Organization","name":"KOROŠAK d.o.o."}</script>'''
        f = FakeFetcher({url: response(html, url)})
        result = verify(company, url, f)
        self.assertFalse(result['verified'])
        self.assertEqual(result['status'], 'REVIEW')
        self.assertEqual(f.requests, 0)

    def test_regional_subdomains_and_equivalent_platforms(self):
        for host in ('mascus.si', 'mascus.de', 'mascus.com', 'seller.mascus.co.uk',
                     'machineryline.info', 'machinio.com', 'www.machinerytrader.com'):
            url = f'https://{host}/dealer/alfa'
            with self.subTest(host=host):
                self.assertTrue(blocked_url(url))
                self.assertFalse(verify(COMPANY, url, FakeFetcher({url: response(IDENTITY, url)}))['verified'])
        self.assertFalse(blocked_url('https://mascus-parts.si/'))
        self.assertFalse(blocked_url('https://mascus.co.uk.dealer.example/'))

    def test_redirect_to_marketplace_cannot_verify(self):
        f = FakeFetcher({'https://alfa.si/': response(IDENTITY, 'https://mascus.si/dealer/alfa')})
        result = verify(COMPANY, 'https://alfa.si/', f)
        self.assertEqual(result['status'], 'REVIEW')
        self.assertFalse(result['verified'])
        self.assertEqual(f.requests, 1)

    def test_unknown_marketplace_profile_overrides_exact_identifiers_and_schema(self):
        url = 'https://equipment-hub.example/dealers/alfa'
        html = '<title>ALFA dealer profile</title> Online equipment marketplace Verified seller ' + IDENTITY
        result = verify(COMPANY, url, FakeFetcher({url: response(html, url)}))
        self.assertEqual(result['status'], 'REVIEW')
        self.assertFalse(result['verified'])
        signals = result['evidence'][0]['signals']
        self.assertIn('third_party_marketplace_page', signals)
        self.assertIn('tax_exact', signals)
        self.assertIn('organization_name_exact', signals)

    def test_first_party_manufacturer_and_dealer_products_remain_verified(self):
        for url in ('https://alfa.si/products', 'https://industrial-brand.example/products'):
            html = '<title>ALFA machinery for sale</title> Equipment dealer. Buy products. Website operated by ALFA d.o.o. ' + IDENTITY
            self.assertTrue(verify(COMPANY, url, FakeFetcher({url: response(html, url)}))['verified'])
        url = 'https://alfa.si/about'
        html = '<title>ALFA dealer profile</title> Also visit our online marketplace. ' + IDENTITY
        self.assertTrue(verify(COMPANY, url, FakeFetcher({url: response(html, url)}))['verified'])

    def test_marketplace_email_crawl_is_rejected_even_if_marked_verified(self):
        for url in ('https://mascus.co.uk/', 'https://machinio.com/'):
            with self.assertRaises(ValueError):
                crawl_site({**COMPANY, 'status': 'VERIFIED', 'website': url}, FakeFetcher({}))
        url = 'https://equipment-hub.example/dealers/alfa'
        html = '<title>ALFA seller profile</title> Online marketplace Verified seller ' + IDENTITY + ' info@equipment-hub.example'
        result = crawl_site({**COMPANY, 'status': 'VERIFIED', 'website': url, 'ownership': {'status':'VERIFIED','scope':'https://equipment-hub.example/'}}, FakeFetcher({url: response(html, url)}))
        self.assertEqual(result['emails'], [])
        self.assertTrue(any('Third-party' in e for e in result['errors']))

    def test_persistence_rejects_marketplace_source_and_targeted_repair(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.initialize_database(); db.initialize_discovery_database(); db.save_company(COMPANY)
            db.save_company({**COMPANY, 'tax_number': '87654321', 'registration_number': '1234567'})
            db.save_website({'company_id': 1, 'status': 'VERIFIED', 'final_url': 'https://mascus.co.uk/'})
            db.save_website({'company_id':2,'status':'VERIFIED','ownership':{'status':'VERIFIED','scope':'https://alfa.si/'}})
            with db.get_connection() as conn:
                for website, source in (('https://mascus.co.uk/', 'https://mascus.co.uk/dealer'),
                                        ('https://alfa.si/', 'https://machinio.com/dealer')):
                    with self.assertRaises(ValueError):
                        db.save_email(conn, 1, 'info@alfa.si', 90, source, 'Seller', 'text', website=website)
                db.save_email(conn, 2, 'info@alfa.si', 90, 'https://alfa.si/', 'Contact', 'text', website='https://alfa.si/', evidence={'publication':'ALFA d.o.o. info@alfa.si'})
                # Simulate a historical record accepted before this repair.
                conn.execute("INSERT INTO email_discovery (company_id,email,website,page_url) VALUES (1,'info@mascus.co.uk','https://mascus.co.uk/','https://mascus.co.uk/dealer')")
                conn.commit()
            self.assertEqual(db.invalidate_blocked_discovery_records([1]), {'websites': 1, 'emails': 1})
            with db.get_connection() as conn:
                self.assertEqual(conn.execute('SELECT status FROM website_discovery WHERE company_id=1').fetchone()[0], 'REVIEW')
                self.assertEqual([tuple(r) for r in conn.execute('SELECT company_id,email FROM email_discovery')], [(2, 'info@alfa.si')])


if __name__ == '__main__': unittest.main()
