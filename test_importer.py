import requests

with open("泷泽由佳 - 2026-04-25@17h40m44s.json", "rb") as f:
    response = requests.post(
        "http://localhost:8001/import",
        files={"file": ("chat.json", f, "application/json")},
    )

print(response.json())
