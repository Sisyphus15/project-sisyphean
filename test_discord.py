import requests

# Put your Discord webhook URL between the quotes:
WEBHOOK_URL = "INSERT WEBHOOK HERE"

data = {
    "content": ":muscle: Proxmox Chad has entered the Chat :muscle:!"
}

response = requests.post(WEBHOOK_URL, json=data)

print("Status code:", response.status_code)
print("Response:", response.text)
