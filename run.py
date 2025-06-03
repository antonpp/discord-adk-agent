import os
import uuid
import json
import asyncio

# --- Environment & Framework Imports ---
from dotenv import load_dotenv
import discord
import requests
from fastapi import FastAPI
import uvicorn

# --- Google Cloud Auth Imports ---
import google.auth.transport.requests
import google.oauth2.id_token


# Load environment variables from a .env file for local development
load_dotenv()

# --- Configuration ---
ADK_BASE_URL = os.getenv('ADK_BASE_URL')
ADK_APP_NAME = os.getenv('ADK_APP_NAME')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_API_KEY')

# A simple in-memory store for user sessions.
user_sessions = {}

# --- FastAPI App Definition ---
# This minimal web server's job is to respond to Cloud Run's health checks.
app = FastAPI(title="Discord Bot Health Check Server")

@app.get("/", summary="Health Check Endpoint")
async def health_check():
    """
    This endpoint is called by Cloud Run to verify the container is live.
    It returns the bot's current connection status.
    """
    return {
        "status": "alive",
        "bot_is_ready": client.is_ready()
    }

# --- Google Cloud Auth Helper ---

def _get_authenticated_headers() -> dict:
    """
    Fetches a Google-signed ID token and returns authorized HTTP headers.
    The 'audience' is the URL of the receiving Cloud Run service.
    """
    headers = {"Content-Type": "application/json"}
    
    # Only attempt to get a token if the ADK_BASE_URL is set
    if not ADK_BASE_URL:
        return headers

    try:
        auth_req = google.auth.transport.requests.Request()
        # Fetches the token from the container's metadata server
        id_token = google.oauth2.id_token.fetch_id_token(auth_req, ADK_BASE_URL)
        headers["Authorization"] = f"Bearer {id_token}"
        print("Successfully fetched ID token for authentication.")
    except Exception as e:
        # This might happen during local development if you haven't authenticated gcloud
        print(f"CRITICAL: Could not fetch Google ID token. Requests to ADK will likely fail. Error: {e}")
    
    return headers


# --- ADK API Interaction Functions (Now with Auth) ---

async def create_adk_session(discord_user_id: str):
    """Creates a new session for a given Discord user with the ADK agent."""
    adk_user_id = f"discord_{discord_user_id}"
    adk_session_id = str(uuid.uuid4())
    url = f"{ADK_BASE_URL}/apps/{ADK_APP_NAME}/users/{adk_user_id}/sessions/{adk_session_id}"
    
    # Get headers with Authorization token
    headers = _get_authenticated_headers()
    
    payload = {"state": {"discord_user_id": str(discord_user_id)}}

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        response.raise_for_status()
        print(f"Successfully created ADK session: {adk_session_id} for user {adk_user_id}")
        return adk_user_id, adk_session_id
    except requests.exceptions.RequestException as e:
        print(f"Error creating ADK session for {adk_user_id}: {e}")
        return None, None

async def send_query_to_adk(adk_user_id: str, adk_session_id: str, user_message: str):
    """Sends a query to the ADK agent and gets the response."""
    url = f"{ADK_BASE_URL}/run"
    
    # Get headers with Authorization token
    headers = _get_authenticated_headers()
    
    payload = {
        "appName": ADK_APP_NAME,
        "userId": adk_user_id,
        "sessionId": adk_session_id,
        "newMessage": {
            "role": "user",
            "parts": [{"text": user_message}]
        }
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        response.raise_for_status()
        response_data = response.json()
        print(f"Received ADK response: {json.dumps(response_data, indent=2)}")

        for item in reversed(response_data):
            if item.get("content") and item.get("content").get("role") == "model":
                parts = item["content"].get("parts", [])
                for part in parts:
                    if "text" in part:
                        return part["text"]
        return "Sorry, I couldn't understand the response from the agent."
    except requests.exceptions.RequestException as e:
        print(f"Error sending query to ADK for user {adk_user_id}, session {adk_session_id}: {e}")
        user_sessions.clear()  # If downstream cloud run timed out (10min) and cleared sessions TODO: add session persistence
        return "Oops. There was an error. Re-sending your question usually fixes it. (It's a session management bug)."
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error parsing ADK response: {e}")
        return "Sorry, I received an unexpected response from the support agent."


# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.messages = True
intents.dm_messages = True # Explicitly enable DM messages intent
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'We have logged in as {client.user}')
    print('Bot is ready to receive messages in DMs.')

@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Only respond to messages in DMs (Direct Messages)
    if not isinstance(message.channel, discord.DMChannel):
        return

    content_to_send = message.content

    if not content_to_send:
        # This case is rare in DMs but good to handle
        await message.channel.send("Hello! How can I help you today?")
        return

    discord_user_id = message.author.id
    adk_user_id_str = f"discord_{discord_user_id}"

    adk_session_id = user_sessions.get(discord_user_id)
    current_adk_user_id = None

    if adk_session_id:
        current_adk_user_id = adk_user_id_str
        print(f"Using existing ADK session {adk_session_id} for user {discord_user_id}")
    else:
        print(f"No active ADK session for user {discord_user_id}. Creating a new one.")
        created_user_id, created_session_id = await create_adk_session(str(discord_user_id))
        if created_session_id:
            user_sessions[discord_user_id] = created_session_id
            adk_session_id = created_session_id
            current_adk_user_id = created_user_id
        else:
            await message.channel.send("Sorry, I couldn't create a new support session. Please try again later.")
            return

    if not current_adk_user_id or not adk_session_id:
        await message.channel.send("There was an issue with your session. Please try again.")
        return

    async with message.channel.typing():
        adk_response = await send_query_to_adk(current_adk_user_id, adk_session_id, content_to_send)
        await message.channel.send(adk_response)


# --- Concurrent Startup Logic ---
async def start_bot():
    """A coroutine to start the discord bot."""
    await client.start(DISCORD_BOT_TOKEN)

async def start_server():
    """A coroutine to start the Uvicorn server."""
    port = int(os.environ.get("PORT", 8080))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_API_KEY environment variable not set.")
    
    # Use asyncio to run both the bot and the web server concurrently
    loop = asyncio.get_event_loop()
    loop.create_task(start_bot())
    loop.create_task(start_server())
    loop.run_forever()