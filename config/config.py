import os

base_url = os.getenv('BASE_URL', 'stg.client-env.com')

url = {
    'url': f'https://{base_url}',
    'login': f'https://backoffice.{base_url}/login/',
    'bet': f'https://backoffice.{base_url}/admin/management/test/casino-bet/',
    'deposit': f'https://backoffice.{base_url}/admin/management/test/deposit/',
    'games': f'https://backoffice.{base_url}/admin/game/search/',
    'total_players': f'https://backoffice.{base_url}/admin/table/render/?page=1&sort_field=&sort_order=&resultsPerPage=30&ignoreAdmins=1&entity=User&template_name=_table-all-players.html.twig&term=&user_id=&regulatory_external_id=&createdAtDateRange=&lastUpdatedAtRange=&kyc_level=&country=&status=&segmentation=&tag=&lastLoginDateRange=&ip_address=&last_ip_address=&last_login_country=&first_name=&last_name=&personal_number=&identification_number=&city=&street_address=&postal_code=&date_of_birth=&gender=&from_total_deposits=&to_total_deposits=&from_amount=&to_amount=&from_total_withdrawals=&to_total_withdrawals=&from_amount_withdrawal=&to_amount_withdrawal=&from_ggr=&to_ggr=&from_ggr_casino=&to_ggr_casino=&from_ggr_sport=&to_ggr_sport=&from_total_bets_amount=&to_total_bets_amount=&from_wins_amount=&to_wins_amount=&from_bonus_claimed=&to_bonus_claimed=&from_bonus_released=&to_bonus_released=&social_login=&from_total_bets=&to_total_bets=&from_total_wins=&to_total_wins=&from_total_tournament_buy_ins_fiat=&to_total_tournament_buy_ins_fiat=&from_total_tournament_prizes=&to_total_tournament_prizes=&source_of_income=',
}

deposit = {
    'user_id': 1,
    'amount_coin': 100,
    'payment_method': '',
    'currency': 5
}

creds = {
    '_username': 'qa@client-env.com',
    '_password': '***********'
}

game_name = [
    "2Wild2Die - Hacksaw Gaming",
    "Cash Crew - Hacksaw Gaming",
    "Dawn of Kings - Hacksaw Gaming",
    "Duel at Dawn - Hacksaw Gaming"
]

# ==========================================
#             FRONTOFFICE URLs
# ==========================================

fo_base_url = os.getenv('FO_BASE_URL', 'qa.client-env.com')

fo_url = {
    'registration': f'https://{fo_base_url}/register/',
    'register_submit': "/utility/post/register/",
    'login': f'https://{fo_base_url}/login/',
    'login_submit': f'https://{fo_base_url}/login/',
    'logout': f'https://{fo_base_url}/logout/'
}
