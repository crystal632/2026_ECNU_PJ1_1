"""MongoDB connection and questionnaire collections."""
import os
from pymongo import MongoClient

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017/")
DATABASE_NAME = os.getenv("MONGO_DATABASE", "questionnaire_db")

client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
db = client[DATABASE_NAME]
users_collection = db["users"]
surveys_collection = db["surveys"]
responses_collection = db["responses"]
questions_collection = db["questions"]
question_banks_collection = db["question_banks"]
