from dotenv import dotenv_values
config = dotenv_values(".env")

ALPACA_KEY_ID = config['key_id']
ALPACA_SECRET_KEY = config['secret_key']
