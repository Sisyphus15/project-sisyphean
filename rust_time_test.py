import json
import asyncio
from rustplus import RustSocket


async def main():
    # Load config
    with open("rust_config.json", "r") as f:
        cfg = json.load(f)

    server_ip = cfg["server_ip"]
    server_port = cfg["server_port"]
    player_id = cfg["player_id"]
    player_token = cfg["player_token"]

    if not server_ip or player_id == 0 or player_token == 0:
        print("❌ Please fill in server_ip, player_id, and player_token in rust_config.json")
        return

    # Connect to Rust+ socket
    print(f"Connecting to {server_ip}:{server_port} as player {player_id} ...")

    async with RustSocket(
        server_ip,
        server_port,
        player_id,
        player_token,
        use_ssl=False  # most servers use plain Rust+ socket
    ) as rust:
        try:
            info = await rust.get_time()
            print("✅ Connected!")
            print(f"In-game time: {info.time:.2f}, day length: {info.day_length}")
        except Exception as e:
            print("❌ Error talking to Rust+:", e)


if __name__ == "__main__":
    asyncio.run(main())
