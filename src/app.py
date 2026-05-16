import random
import secrets
import string

import aiohttp
import requests
from bs4 import BeautifulSoup

from config import config
from scenarios.utils.bet_request import bet


def generate_token(length=40):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


async def fetch_cookies():
    session = requests.Session()
    session.get(config.url['url'])
    cookies = session.cookies.get_dict()

    return '; '.join(f'{k}={v}' for k, v in cookies.items())


async def login():
    jar = aiohttp.CookieJar()

    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.get(config.url['login']) as resp:
            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")

            csrf = soup.find("input", {"name": "_csrf_token"})
            if not csrf:
                raise Exception("CSRF token not found")
            csrf_value = csrf["value"]

        credentials = config.creds.copy()
        credentials["_csrf_token"] = csrf_value

        async with session.post(config.url['login'], data=credentials, allow_redirects=False) as resp:
            html = await resp.text()
            if resp.status != 302:
                print("[login] Login POST did not redirect. Status:", resp.status)
                print("[login] Response snippet:", html[:200])
                return None

            loc = resp.headers.get('Location')
            if loc == '/login/' or loc is None:
                print("[login] Login failed or redirected back to login page")
                return None

            jar_cookies = {c.key: c.value for c in jar}
            cookie_header = "; ".join(f"{k}={v}" for k, v in jar_cookies.items())
            return cookie_header


async def get_total_players(cookie: str, session: aiohttp.ClientSession) -> int | None:
    headers = {
        'Cookie': f'{cookie};',
        'Accept': 'application/json'
    }

    try:
        async with session.get(config.url['total_players'], headers=headers) as resp:
            data = await resp.json()
            total_players = data.get('totalCount')
            if total_players is None:
                print("Total players not found in response")
                return None
            return int(total_players)
    except Exception as e:
        print(f"Error fetching total players: {e}")
        return None


async def deposit(user_id: int, cookie: str, session: aiohttp.ClientSession) -> bool:
    headers = {
        'Cookie': f'{cookie};',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json'
    }

    deposit_data = config.deposit.copy()
    deposit_data['user_id'] = user_id

    async with session.post(config.url['deposit'], data=deposit_data, headers=headers) as resp:
        try:
            json_data = await resp.json()
            if json_data.get('success'):
                print(f"Deposit successful for user {user_id}: balance {json_data['user']['balance']}")
                return True
            else:
                print(f"Deposit failed for user {user_id}: status={resp.status}, response={json_data.get('message')}")
                return False
        except aiohttp.ContentTypeError:
            text = await resp.text()
            print(f"Deposit error for user {user_id}: status={resp.status}, response={text[:200]}")
            return False
        except Exception as e:
            print(f"Deposit exception for user {user_id}: {type(e).__name__}: {e}")
            return False


async def get_available_games(cookie: str, session: aiohttp.ClientSession) -> dict[str, int] | None:
    headers = {
        'Cookie': f'{cookie};',
        'Accept': 'application/json'
    }

    try:
        async with session.get(config.url['games'], headers=headers) as resp:
            json_data = await resp.json()
            items = json_data.get('items', [])

            available_games = {}
            for game_name in config.game_name:
                for game in items:
                    if game.get('name') == game_name:
                        game_id = game.get('id')
                        available_games[game_name] = game_id
                        break

            missing_games = set(config.game_name) - set(available_games.keys())
            if missing_games:
                print(f"WARNING: Missing games from configuration: {missing_games}")

            if not available_games:
                print(f"No games found from {config.game_name}")
                return None

            print(f"Available games: {available_games}")
            return available_games
    except Exception as e:
        print(f"Error fetching games: {e}")
        return None


async def get_game_id(cookie: str, session: aiohttp.ClientSession) -> int | None:
    available_games = await get_available_games(cookie, session)

    if not available_games:
        return None

    game_name = random.choice(list(available_games.keys()))
    game_id = available_games[game_name]
    print(f"Selected random game: '{game_name}' with id {game_id}")
    return game_id


async def make_bet_request(user_id, token: str, cookie: str, game_id: int, session: aiohttp.ClientSession) -> tuple[
    dict, bool]:
    headers = {
        'Cookie': f'{cookie};',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'close',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json'
    }

    async with session.post(config.url['bet'], data=bet(user_id, token, game_id), headers=headers) as resp:
        try:
            json_data = await resp.json()
            print("Bet response:", json_data)
            is_success = isinstance(json_data, dict) and json_data.get('success') is True
            return json_data, is_success
        except aiohttp.ContentTypeError:
            error_text = await resp.text()
            print("Expected JSON, got:", error_text)
            return {"error": error_text}, False
        except Exception as e:
            print(f"Bet error: {e}")
            return {"error": str(e)}, False
