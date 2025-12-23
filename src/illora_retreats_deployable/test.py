import gspread
from google.oauth2.service_account import Credentials

creds = Credentials.from_service_account_file(
    "GOOGLE_SERVICE_ACCOUNT_JSON.json",
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)

client = gspread.authorize(creds)

sheet = client.open_by_key("1Wv7lOg8yjsK12hve4CauZFTL_hVJrwY3j7MObW_2q1E")
worksheet = sheet.sheet1

print("Connected! First row:", worksheet.row_values(1))
