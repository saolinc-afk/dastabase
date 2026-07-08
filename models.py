from dataclasses import dataclass


@dataclass
class Company:
    company_name: str
    address: str
    registration_number: str
    tax_number: str
    activity: str
    phone: str
    website: str
    email: str