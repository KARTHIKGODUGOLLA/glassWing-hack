"""Run ShiftVoice: python app.py, then open http://localhost:8000"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("src.server:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
