from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import random
import time

app = FastAPI()

# CORS settings for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sample screening logic
@app.get("/api/screen")
def screen_stocks():
    # Simulate processing time
    time.sleep(1)
    
    # Sample stock data symbols
    symbols = ["7203", "6758", "9984", "8035", "6098", "4063", "6501", "7751", "6902", "8001"]
    names = ["トヨタ", "ソニーG", "ソフトバンクG", "東エレク", "リクルート", "信越化", "日立", "キヤノン", "デンソー", "伊藤忠"]
    
    results = []
    for i in range(len(symbols)):
        # Randomly pick some stocks to match the "screening"
        if random.random() > 0.4:
            results.append({
                "code": symbols[i],
                "name": names[i],
                "price": random.randint(1000, 15000),
                "change": round(random.uniform(-5, 5), 2),
                "per": round(random.uniform(5, 30), 1),
                "pbr": round(random.uniform(0.5, 5.0), 2)
            })
    
    return {"results": results}

# Serve static files for the frontend
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
