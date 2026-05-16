def bet(user_id, token, game_id):
    return {
        "game_token": token,
        'user_id': user_id,
        'bet_amount': 1,
        'profit_amount': 5,
        'currency': 5,
        'game_id': game_id
    }
