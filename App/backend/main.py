import os
from dotenv import load_dotenv

import secrets

import json
import redis

from datetime import datetime
import requests

from pymongo import MongoClient

from flask_session import Session
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
import google.generativeai as genai
from google.ai.generativelanguage_v1beta.types import content


# Load environment variables
load_dotenv()

# Configure API key
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Define the model and response schema
generation_config = {
    "temperature": 1,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
    "response_schema": content.Schema(
        type=content.Type.OBJECT,
        properties={
            "category": content.Schema(type=content.Type.STRING),
            "probability": content.Schema(type=content.Type.NUMBER),
        },
    ),
    "response_mime_type": "application/json",
}

# Initialize Gemini model
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash-8b",
    generation_config=generation_config,
)


def categorize_posts(posts):
    """Categorizes posts one by one using Gemini AI with JSON schema enforcement."""
    if not posts:
        return {}

    categorized_posts = {}

    for post in posts:
        post_id = str(post["tweet"]["tweet_id"])
        post_text = post["tweet"]["tweet_content"]

        prompt = f"""
        Classify the following post into one of the categories:
        1. Harassment
        2. Threat
        3. Inappropriate
        4. Safe

        Post: "{post_text}"

        Respond strictly in JSON format:
        {{
            "category": "...",
            "probability": 0.0
        }}
        """

        try:
            # Start chat session
            chat_session = model.start_chat(
                history=[
                    {"role": "user", "parts": [prompt]},
                    {"role": "model", "parts": ["```json\n {\n  \"category\": \"Safe\",\n  \"probability\": 0.9\n}\n```"]},
                ]
            )

            # Send request to Gemini API
            response = chat_session.send_message(prompt)

        
            # Ensure response is not empty
            if not response.text.strip():
                raise ValueError("Received empty response from Gemini API")

            # Parse the JSON response
            response_data = json.loads(response.text)

            # Validate expected keys in response
            if "category" not in response_data or "probability" not in response_data:
                raise ValueError("Missing expected keys in Gemini response")

            # Store categorized post
            categorized_posts[post_id] = {
                "post_text": post_text,
                "category": response_data["category"],
                "probability": response_data["probability"],
            }

        except json.JSONDecodeError:
            categorized_posts[post_id] = {
                "post_text": post_text,
                "category": "Error",
                "probability": 0.0,
            }

        except Exception as e:
            categorized_posts[post_id] = {
                "post_text": post_text,
                "category": "Error",
                "probability": 0.0,
            }

    return categorized_posts


app = Flask(__name__, static_folder="../assets", template_folder="../frontend")

# App configuration
app_config = {
    "SECRET_KEY": os.getenv("SECRET_KEY"),  # Ensure a default value
    "TEMPLATES_AUTO_RELOAD": True,
    "SESSION_TYPE": "redis",
    "SESSION_REDIS": redis.from_url(os.getenv("REDIS_URI")),
    "SESSION_PERMANENT": False,
    "SESSION_USE_SIGNER": True,
    "SESSION_COOKIE_SECURE": os.getenv("FLASK_ENV", "production") == "production",
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_NAME": "innovxchange-session",
    "SESSION_COOKIE_HTTPONLY": True,
}

# Apply configuration to the app
app.config.from_mapping(app_config)

# Initialize Flask-Session
Session(app)

# Initialize MongoDB client
MONGO_CLIENT = MongoClient(os.getenv("MONGODB_URI"))["cyber-skahi-prod"]


# Routes
@app.route("/")
def index():
    if session.get("user"):
        if MONGO_CLIENT["USERS"].find_one(
            {"user_id": session["user"]["user_id"], "account_info.social_profile": {"$exists": True}}
        ):
          return redirect(url_for("dashboard"))
        else:
          return render_template("index.html")
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))

    user_info = MONGO_CLIENT["USERS"].find_one({"user_id": user["user_id"]})
    if not user_info or "account_info" not in user_info or "social_profile" not in user_info["account_info"]:
        return redirect(url_for("login"))

    # Fetch posts mentioning the user
    # Fetch posts mentioning the user's username OR name

    all_posts = list(MongoClient(os.getenv("SOCIAL_MEDIA_MONGODB_URI"))["flutterbird"]["posts"].find({
        "$or": [
            {"tweet.tweet_content": {"$regex": f"@{user['username']}", "$options": "i"}},
            {"tweet.tweet_content": {"$regex": f"{user['name']}", "$options": "i"}},
            {"tweet.tweet_content": {"$regex": f"{user['name'].split()[0]}", "$options": "i"}},
            {"tweet.tweet_content": {"$regex": f"{user['name'].split()[1]}", "$options": "i"}},
        ]
    }, {"_id": 0, "tweet.tweet_id": 1, "tweet.tweet_content": 1}))


    # Process posts with Gemini Flash 2.0
    categorized_posts = categorize_posts(all_posts)

    # Add post user info
    for post_id, post in categorized_posts.items():
        user_info = MongoClient(os.getenv("SOCIAL_MEDIA_MONGODB_URI"))["flutterbird"]["posts"].find_one(
            {"tweet.tweet_id": post_id}, {"_id": 0, "user": 1}
        )

        if user_info:
            post["user"] = user_info["user"]

    return render_template("dashboard.html", user_info=user_info, categorized_posts=categorized_posts)


# Authentication routes
@app.route("/auth/sign-in", methods=["GET"])
def login():
    session["auth_state"] = secrets.token_hex(16)
    return redirect(f'https://accounts.om-mishra.com/api/v1/oauth2/authorize?client_id=142decc8-6147-40d5-b5d7-3f2bec416c0a&state={session["auth_state"]}')

@app.route("/auth/sign-out", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/oauth/_handler", methods=["GET"])
def callback():
    code = request.args.get("code")
    if not code:
        return redirect(
            url_for(
                "index",
                message="The authentication attempt failed, due to missing code parameter!",
            )
        )

    if request.args.get("state") != session.get("auth_state"):
        return redirect(
            url_for(
                "index",
                message="The authentication attempt failed, due to mismatched state parameter!",
            )
        )

    oauth_response = requests.post(
        "https://accounts.om-mishra.com/api/v1/oauth2/user-info",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={
            "client_id": os.getenv("CLIENT_ID"),
            "client_secret": os.getenv("CLIENT_SECRET"),
            "code": code,
        },
    )

    if oauth_response.status_code != 200:
        return redirect(
            url_for(
                "index",
                message="The authentication attempt failed, due to invalid response from GitHub!",
            )
        )
        
    user_data = oauth_response.json()["user"]

    if (
        MONGO_CLIENT["USERS"].find_one({"account_info.oauth_id": user_data["user_public_id"]}) is None
    ):
        MONGO_CLIENT["USERS"].insert_one(
            {
                "user_id": user_data["user_public_id"],
                "user_info": {
                    "username": user_data["user_profile"]["user_name"],
                    "name": user_data["user_profile"]["user_display_name"],
                    "avatar_url": user_data["user_profile"]["user_profile_picture"],
                },
                "account_info": {
                    "oauth_provider": "om-mishra",
                    "oauth_id": user_data["user_public_id"],
                    "created_at": datetime.now(),
                    "last_login": datetime.now(),
                    "is_active": True,
                },
            }
        )
    else:
        user_id = MONGO_CLIENT["USERS"].find_one(
            {"account_info.oauth_id": user_data["user_public_id"]}
        )["user_id"]

        MONGO_CLIENT["USERS"].update_one(
            {"account_info.oauth_id": user_data["user_public_id"]},
            {"$set": {"account_info.last_login": datetime.now(), "user_info.avatar_url": user_data["user_profile"]["user_profile_picture"]}},
        )

    user_info = MONGO_CLIENT["USERS"].find_one({"user_id": user_data["user_public_id"]})

    session["user"] = {
        "user_id": user_info["user_id"],
        "username": user_info["user_info"]["username"],
        "name": user_info["user_info"]["name"],
        "avatar_url": f"{user_info['user_info']['avatar_url']}",
    }

    return redirect(url_for("index"))


# API routes

@app.route("/api/v1/check-profile", methods=["POST"])
def check_profile():
    account_username = request.json.get("profile").strip().replace("@", "")

    user_profile = MongoClient(os.getenv("SOCIAL_MEDIA_MONGODB_URI"))["flutterbird"]["users"].find_one(
        {"user_name": account_username}, {"_id": 0}
    )


    if user_profile is None:
        return jsonify({"status": "error", "message": "The user profile does not exist, please check the username!"})
    
    MONGO_CLIENT["USERS"].update_one(
        {"user_id": session["user"]["user_id"]},
        {"$set": {"account_info.social_profile": user_profile}},
    )

    return jsonify({"status": "success", "message": "The user profile has been successfully linked!"})

if __name__ == "__main__":
    app.run(debug=True)