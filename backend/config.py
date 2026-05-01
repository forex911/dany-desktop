import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Flask settings
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    
    # Download settings
    MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
    ALLOWED_EXTENSIONS = {'.mp4', '.mp3', '.webm', '.m4a'}
    
    # Cleanup settings
    MAX_FILE_AGE_HOURS = 1  # Delete files older than 1 hour
    
    # Rate limiting (optional - implement with flask-limiter)
    RATE_LIMIT = "10 per minute"