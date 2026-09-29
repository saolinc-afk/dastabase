"""Phase B P0 contact correctness regressions; all evidence is offline."""
import json
import unittest
from unittest.mock import patch

from discovery_v2.contacts import resolve_contacts, select_default
from discovery_v2.evidence import EvidenceWriter
from discovery_v2.tests.test_live_regressions import MemoryStore
from discovery_v2.tests.test_phase_a import response


class P0ContactTests(unittest.TestCase):
    def setUp(self):
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.company = dict(id=1, company_name='PRIMER d.o.o.', tax_number='12345678',
                            registration_number='5591872', address='Glavna 1, 1000 Ljubljana',
                            municipality='Ljubljana')

    def extract(self, html, company=None, url='https://primer.si/kontakt'):
        company = company or self.company
        writer = EvidenceWriter(MemoryStore(), {}, company)
        page = response('<title>PRIMER</title><h1>PRIMER d.o.o.</h1>' + html, url)
        writer.page(url, page)
        contacts = resolve_contacts(company, writer.observations,
                                    dict(status='VERIFIED', scope='https://primer.si/'), {url: page})
        return writer, {c['normalized_value']: c for c in contacts}

    def selected(self, contacts, kind):
        contact_id = select_default(list(contacts.values()), kind)
        return next((c for c in contacts.values() if c['contact_id'] == contact_id), None)

    def test_registration_and_tax_numbers_cannot_become_phones(self):
        writer, contacts = self.extract('''<section><h2>Kontakt PRIMER d.o.o.</h2>
            <p>Matična številka: 5591872; Telefon: 5591872</p>
            <p>Davčna številka: SI12345678; Telefon: 12345678</p>
            <p>Telefon: +386 1 234 56 78</p></section>''')
        self.assertNotIn('5591872', contacts)
        self.assertNotIn('12345678', contacts)
        self.assertIn('+38612345678', contacts)
        exclusions = [o for o in writer.observations if o['observation_type'] == 'PHONE_EXCLUSION']
        self.assertEqual({o['value']['exclusion_reason'] for o in exclusions},
                         {'MATCHES_REGISTRATION_NUMBER', 'MATCHES_TAX_NUMBER'})

    def test_locally_labelled_company_and_vat_ids_are_excluded(self):
        company = {**self.company, 'tax_number': '', 'registration_number': ''}
        writer = EvidenceWriter(MemoryStore(), {}, company)
        writer.search_result({'url': 'https://primer.si/', 'title': 'PRIMER d.o.o.',
            'body': 'Company ID: 7654321 Telefon: 7654321; VAT: 87654321 Telefon: 87654321'},
            'LEGAL_COMPANY_CONTACT', 'offline', 'fixture', 1)
        self.assertFalse(any(o['observation_type'] == 'PHONE_CANDIDATE' for o in writer.observations))
        self.assertEqual(len([o for o in writer.observations if o['observation_type'] == 'PHONE_EXCLUSION']), 2)

    def test_foreign_office_phone_is_additional_not_default(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Glavni telefon: +386 1 234 56 78</p>
            <p>Foreign office Austria, mobilni telefon: +43 1 555 01 00</p></section>''')
        self.assertEqual(contacts['+4315550100']['attribution_status'], 'ATTRIBUTED')
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38612345678')
        basis = json.loads(contacts['+4315550100']['role_basis_json'])
        self.assertFalse(basis['country_context']['target_country_match'])

    def test_plus_and_double_zero_foreign_prefixes_are_equivalent(self):
        for prefix, expected in (('+381', '+38115550100'), ('00381', '+38115550100'),
                                 ('+43', '+4315550100'), ('0043', '+4315550100')):
            with self.subTest(prefix=prefix):
                _, contacts = self.extract(f'''<section><h2>PRIMER d.o.o.</h2>
                    <p>Glavni telefon: +386 1 234 56 78</p>
                    <p>Foreign office, telefon: {prefix} 1 555 01 00</p></section>''')
                self.assertIn(expected, contacts)
                self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38612345678')
                basis = json.loads(contacts[expected]['role_basis_json'])
                self.assertFalse(basis['country_context']['target_country_match'])

    def test_foreign_context_penalizes_unknown_country_number(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Glavni telefon: +386 1 234 56 78</p>
            <p>Foreign office, telefon: 01 555 01 00</p></section>''')
        self.assertEqual(contacts['015550100']['attribution_status'], 'ATTRIBUTED')
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38612345678')
        basis = json.loads(contacts['015550100']['role_basis_json'])
        self.assertIsNone(basis['country_context']['target_country_match'])
        self.assertTrue(basis['country_context']['foreign_context'])

    def test_explicit_target_primary_foreign_phone_can_win(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>PRIMER d.o.o. Glavni telefon: +43 1 555 01 00</p></section>''')
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+4315550100')

    def test_calling_code_metadata_does_not_claim_a_parsed_code(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>PRIMER d.o.o. Glavni telefon: +1 212 555 0100</p>
            <p>Telefon: +43 1 555 01 00</p><p>Telefon: +386 1 234 56 78</p></section>''')
        for number, match in (('+12125550100', False), ('+4315550100', False), ('+38612345678', True)):
            context = json.loads(contacts[number]['role_basis_json'])['country_context']
            self.assertEqual(context['target_country_match'], match)
            self.assertNotIn('observed_calling_code', context)
            self.assertEqual(context['normalized_international_number'], number)

    def test_switchboard_outranks_department_mobile(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Centrala, telefon: +386 1 234 56 78</p>
            <p>Servis, mobilni telefon: +386 40 222 333</p></section>''')
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38612345678')
        self.assertIn('+38640222333', contacts)

    def test_explicit_main_mobile_can_be_default(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Glavni mobilni telefon: +386 40 222 333</p></section>''')
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38640222333')

    def test_general_email_outranks_transactional_email(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Splošni kontakt <a href="mailto:office@primer.si">office@primer.si</a></p>
            <p>Vračila blaga <a href="mailto:primer.returns@gmail.com">primer.returns@gmail.com</a></p></section>''')
        self.assertEqual(self.selected(contacts, 'EMAIL')['normalized_value'], 'office@primer.si')
        self.assertEqual(contacts['primer.returns@gmail.com']['attribution_status'], 'ATTRIBUTED')

    def test_visible_and_mailto_first_party_general_emails_are_extracted(self):
        _, contacts = self.extract('''<p>info@primer.si</p>
            <p><a href="mailto:office@primer.si">office@primer.si</a></p>''')
        self.assertEqual(contacts['info@primer.si']['attribution_status'], 'ATTRIBUTED')
        self.assertEqual(contacts['office@primer.si']['attribution_status'], 'ATTRIBUTED')

    def test_safe_visible_obfuscation_is_extracted_but_hidden_examples_are_not(self):
        writer, contacts = self.extract('''<p>Kontakt: info (at) primer (dot) si</p>
            <!-- hidden@example.com --><span hidden>template@example.org</span>
            <script>const example = "script@example.com";</script>''')
        self.assertIn('info@primer.si', contacts)
        self.assertTrue(any(o['extraction_method'] == 'visible_obfuscated_text'
                            for o in writer.observations
                            if o['normalized_value'] == 'info@primer.si'))
        self.assertFalse(any('example.' in c for c in contacts))

    def test_dpo_only_is_retained_but_has_no_default(self):
        _, contacts = self.extract('''<section><h2>Varstvo osebnih podatkov</h2>
            <p>PRIMER d.o.o. DPO: <a href="mailto:dpo@primer.si">dpo@primer.si</a></p>
            </section>''')
        self.assertEqual(contacts['dpo@primer.si']['attribution_status'], 'ATTRIBUTED')
        self.assertEqual(json.loads(contacts['dpo@primer.si']['roles_json']), ['PRIVACY'])
        self.assertIsNone(self.selected(contacts, 'EMAIL'))

    def test_explicit_general_context_beats_prefix_and_domain(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>General company contact: <a href="mailto:team@legacy.eu">team@legacy.eu</a></p>
            <p>Sales: <a href="mailto:sales@primer.si">sales@primer.si</a></p></section>''')
        self.assertEqual(self.selected(contacts, 'EMAIL')['normalized_value'], 'team@legacy.eu')
        self.assertEqual(json.loads(contacts['team@legacy.eu']['roles_json']), ['UNKNOWN'])

    def test_unrelated_general_word_does_not_promote_email(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>General information about products <a href="mailto:team@legacy.eu">team@legacy.eu</a></p>
            <p>Sales <a href="mailto:sales@primer.si">sales@primer.si</a></p></section>''')
        self.assertEqual(self.selected(contacts, 'EMAIL')['normalized_value'], 'sales@primer.si')

    def test_reception_outranks_sales_and_sales_remains(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Recepcija <a href="mailto:reception@primer.si">reception@primer.si</a></p>
            <p>Prodaja <a href="mailto:sales@primer.si">sales@primer.si</a></p></section>''')
        self.assertEqual(self.selected(contacts, 'EMAIL')['normalized_value'], 'reception@primer.si')
        self.assertEqual(json.loads(contacts['sales@primer.si']['roles_json']), ['SALES'])
        self.assertEqual(contacts['sales@primer.si']['attribution_status'], 'ATTRIBUTED')

    def test_stronger_cross_domain_context_can_win(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Glavni splošni kontakt <a href="mailto:office@legacy.eu">office@legacy.eu</a></p>
            <p>Prodaja <a href="mailto:sales@primer.si">sales@primer.si</a></p></section>''')
        self.assertEqual(self.selected(contacts, 'EMAIL')['normalized_value'], 'office@legacy.eu')

    def test_headings_supply_bounded_email_roles(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <h3>Reception</h3><p>PRIMER d.o.o. <a href="mailto:frontdesk@legacy.eu">Email</a></p>
            <h3>Sales</h3><p>PRIMER d.o.o. <a href="mailto:sales@primer.si">Email</a></p>
            <h3>Returns</h3><p>PRIMER d.o.o. <a href="mailto:returns@primer.si">Email</a></p>
            <h3>Privacy</h3><p>PRIMER d.o.o. <a href="mailto:privacy@primer.si">Email</a></p></section>''')
        self.assertEqual(self.selected(contacts, 'EMAIL')['normalized_value'], 'frontdesk@legacy.eu')
        ranks = {value: json.loads(contact['role_basis_json'])['default_rank'] for value, contact in contacts.items()}
        self.assertLess(ranks['frontdesk@legacy.eu'], ranks['sales@primer.si'])
        self.assertLess(ranks['sales@primer.si'], ranks['privacy@primer.si'])
        self.assertLess(ranks['privacy@primer.si'], ranks['returns@primer.si'])

    def test_heading_supplies_main_phone_role(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <h3>Centrala</h3><p>PRIMER d.o.o. <a href="tel:+38612345678">Call</a></p>
            <h3>Servis</h3><p>PRIMER d.o.o. <a href="tel:+38640222333">Call</a></p></section>''')
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38612345678')

    def test_each_tel_link_uses_its_own_local_label(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2><p>
            Centrala: <a href="tel:+38612345678">+386 1 234 56 78</a><br>
            Električarji: <a href="tel:+38640222333">+386 40 222 333</a></p></section>''')
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38612345678')
        central = json.loads(contacts['+38612345678']['role_basis_json'])
        department = json.loads(contacts['+38640222333']['role_basis_json'])
        self.assertLess(central['default_rank'], department['default_rank'])

    def test_visible_first_party_international_phone_is_extracted(self):
        _, contacts = self.extract('<section><h2>Kontakt PRIMER d.o.o.</h2><p>+386 1 234 56 78</p></section>')
        self.assertIn('+38612345678', contacts)

    def test_fax_is_rejected_while_labelled_telephone_is_default(self):
        writer, contacts = self.extract('''<section><h2>Kontakt PRIMER d.o.o.</h2><p>
            Telefon: +386 1 89 83 507<br>Fax: +386 1 89 80 002</p></section>''')
        self.assertIn('+38618983507', contacts)
        self.assertNotIn('+38618980002', contacts)
        self.assertFalse(any(o['observation_type'] == 'PHONE_CANDIDATE'
                             and o['normalized_value'] == '+38618980002'
                             for o in writer.observations))
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38618983507')

    def test_fax_only_has_no_phone_default(self):
        writer, contacts = self.extract('''<section><h2>Kontakt PRIMER d.o.o.</h2><p>
            Faks: <span>+386 1 89 80 002</span></p></section>''')
        self.assertFalse(any(o['observation_type'] == 'PHONE_CANDIDATE'
                             for o in writer.observations))
        self.assertIsNone(self.selected(contacts, 'PHONE'))

    def test_separate_tel_and_fax_labels_do_not_contaminate_telephone(self):
        writer, contacts = self.extract('''<section><h2>Kontakt PRIMER d.o.o.</h2><p>
            Telefon: <a href="tel:+38618983507">+386 1 89 83 507</a><br>
            Telefaks: <a href="tel:+38618980002">+386 1 89 80 002</a></p></section>''')
        self.assertIn('+38618983507', contacts)
        self.assertNotIn('+38618980002', contacts)
        self.assertFalse(any(o['observation_type'] == 'PHONE_CANDIDATE'
                             and o['normalized_value'] == '+38618980002'
                             for o in writer.observations))
        self.assertEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+38618983507')

    def test_foreign_office_is_preserved_but_subsidiary_is_not_attributed(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o. Foreign office</h2>
            <p>Telefon: +43 1 555 01 00</p></section>
            <section><h2>Foreign subsidiary BETA GmbH</h2>
            <p>Telefon: +49 30 555 0100</p></section>
            <section><h2>PRIMER d.o.o.</h2><p>Telefon: +386 1 234 56 78</p></section>''')
        self.assertEqual(contacts['+4315550100']['attribution_status'], 'ATTRIBUTED')
        self.assertNotEqual(contacts['+49305550100']['attribution_status'], 'ATTRIBUTED')
        self.assertNotEqual(self.selected(contacts, 'PHONE')['normalized_value'], '+49305550100')
        self.assertEqual(contacts['+38612345678']['attribution_status'], 'ATTRIBUTED')

    def test_identifier_labels_are_token_bounded_and_snapshot_field_is_recorded(self):
        writer, _ = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Private: 87654321; Telefon: 87654321</p>
            <p>VAT: 12345678; Telefon: 12345678</p></section>''')
        tax = next(o for o in writer.observations if o['observation_type'] == 'PHONE_EXCLUSION')
        self.assertEqual(tax['value']['matched_identity_source'],
                         {'kind': 'IDENTITY_SNAPSHOT', 'field': 'tax_number'})
        self.assertTrue(any(o['observation_type'] == 'PHONE_CANDIDATE'
                            and o['normalized_value'] == '87654321' for o in writer.observations))

    def test_winning_ranking_observation_is_recorded(self):
        _, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Returns <a href="mailto:team@legacy.eu">team@legacy.eu</a></p>
            <p>General company contact <a href="mailto:team@legacy.eu">team@legacy.eu</a></p></section>''')
        contact = contacts['team@legacy.eu']
        basis = json.loads(contact['role_basis_json'])
        self.assertIn(basis['ranking_observation_id'], json.loads(contact['supporting_observation_ids_json']))
        self.assertEqual(basis['ranking_reasons'], ['explicit general company contact'])

    def test_unattributed_search_context_cannot_promote_default_rank(self):
        writer, contacts = self.extract('''<section><h2>PRIMER d.o.o.</h2>
            <p>Department <a href="mailto:team@legacy.eu">team@legacy.eu</a></p>
            <p>Sales <a href="mailto:sales@primer.si">sales@primer.si</a></p></section>''')
        writer.search_result({'url': 'https://directory.invalid/primer', 'title': 'PRIMER d.o.o.',
                              'body': 'General company contact team@legacy.eu'},
                             'LEGAL_COMPANY_CONTACT', 'offline', 'fixture', 1)
        contacts = {c['normalized_value']: c for c in resolve_contacts(
            self.company, writer.observations, dict(status='VERIFIED', scope='https://primer.si/'),
            {'https://primer.si/kontakt': response('''<title>PRIMER</title><h1>PRIMER d.o.o.</h1>
                <section><h2>PRIMER d.o.o.</h2><p>Department <a href="mailto:team@legacy.eu">team@legacy.eu</a></p>
                <p>Sales <a href="mailto:sales@primer.si">sales@primer.si</a></p></section>''',
                'https://primer.si/kontakt')})}
        self.assertEqual(self.selected(contacts, 'EMAIL')['normalized_value'], 'sales@primer.si')


if __name__ == '__main__':
    unittest.main()
