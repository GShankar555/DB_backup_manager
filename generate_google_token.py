from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]

flow = InstalledAppFlow.from_client_secrets_file(
    "oauth-client.json",
    SCOPES,
)

credentials = flow.run_local_server(
    port=0,
    access_type="offline",
    prompt="consent",
)

with open("authorized-user.json", "w") as output:
    output.write(credentials.to_json())

print("Created authorized-user.json")