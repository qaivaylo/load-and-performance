import random
import string
from datetime import datetime, timedelta
from faker import Faker

fake = Faker()


class Registration:
    """Helper class to generate random registration data"""

    @staticmethod
    def email():
        """Generate email: qa_{random_str}@qa-ga.com"""
        random_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"qa_{random_str}@qa-ga.com"

    @staticmethod
    def password():
        """Generate random password min 6 chars, with mix of uppercase, lowercase, digits"""
        while True:
            pwd = fake.password(length=10, special_chars=False, digits=True, upper_case=True, lower_case=True)
            if len(pwd) >= 6:
                return pwd

    @staticmethod
    def username():
        """Generate random username"""
        return fake.user_name()

    @staticmethod
    def firstname():
        """Generate random first name"""
        return fake.first_name()

    @staticmethod
    def lastname():
        """Generate random last name"""
        return fake.last_name()

    @staticmethod
    def city():
        """Generate random city"""
        return fake.city()

    @staticmethod
    def state():
        """Generate random Bulgarian state"""
        states = [
            'Blagoevgrad', 'Burgas', 'Dobrich', 'Gabrovo', 'Haskovo', 'Kardzhali', 
            'Kyustendil', 'Lovech', 'Montana', 'Pazardzhik', 'Pernik', 'Pleven', 
            'Plovdiv', 'Razgrad', 'Ruse', 'Shumen', 'Silistra', 'Sliven', 'Smolyan', 
            'Sofia (city)', 'Sofia (region)', 'Stara Zagora', 'Targovishte', 'Varna', 
            'Veliko Tarnovo', 'Vidin', 'Vratsa', 'Yambol'
        ]
        return random.choice(states)

    @staticmethod
    def postal_code():
        """Generate random postal code: 4-11 digit number"""
        length = random.randint(4, 11)
        return ''.join(random.choices(string.digits, k=length))

    @staticmethod
    def identification_number():
        """Generate random 10 digit identification number"""
        return ''.join(random.choices(string.digits, k=10))

    @staticmethod
    def current_asset():
        """Currency - return 5"""
        return "5"

    @staticmethod
    def gender():
        """Return random male or female"""
        return random.choice(["male", "female"])

    @staticmethod
    def phone():
        """Generate random Bulgarian phone number: +359XXXXXXXXXXX"""
        prefix = random.choice([8, 9])
        number = ''.join(random.choices(string.digits, k=8))
        return f"+359{prefix}{number}"

    @staticmethod
    def nationality():
        """Return random number from 1 to 251"""
        return str(random.randint(1, 251))

    @staticmethod
    def state_code():
        """Generate random state code"""
        return str(random.randint(1, 28))

    @staticmethod
    def document_type():
        """Return random: id or passport"""
        return random.choice(["id", "passport"])

    @staticmethod
    def ask_age_confirmation():
        """Age confirmation - return 1"""
        return "1"

    @staticmethod
    def birth_date():
        """Generate random birth date for age >= 18 years in MM/DD/YYYY format"""
        today = datetime.now()
        min_date = today - timedelta(days=365 * 80)
        max_date = today - timedelta(days=365 * 18)
        random_date = fake.date_between(start_date=min_date, end_date=max_date)
        return random_date.strftime('%m/%d/%Y')

    @staticmethod
    def street_address():
        """Generate random street address"""
        return fake.street_address()

    @staticmethod
    def document_number():
        """Generate random document number: 6-12 digit"""
        length = random.randint(6, 12)
        return ''.join(random.choices(string.digits, k=length))

    @staticmethod
    def promo_code():
        """Promo code - return None"""
        return None

    @staticmethod
    def privacy_policy_and_gdpr_consent():
        """Privacy policy consent - return 1"""
        return "1"

    @staticmethod
    def registered_gambling_addict_consent():
        """Gambling addict consent - return 1"""
        return "1"

    @staticmethod
    def politically_exposed_person():
        """Politically exposed person - random 1 or 0"""
        return str(random.choice([0, 1]))

    @staticmethod
    def marketing_consent():
        """Marketing consent - return 1"""
        return "1"

    @staticmethod
    def term_conditions():
        """Term conditions - return 1"""
        return "1"