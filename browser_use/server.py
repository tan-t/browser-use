from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uuid
import asyncio
import aiosqlite
import json
import os
import shutil
from datetime import datetime
from pydantic import BaseModel
from typing import Optional, Dict, Any, Union

from browser_use.agent.service import Agent
from browser_use.agent.views import AgentHistoryList
from browser_use.browser.browser import Browser, BrowserConfig
from langchain_openai import ChatOpenAI

def sanitize_agent_result(data: Any) -> None:
    """
    Recursively remove or nullify 'screenshot' fields from the model_dump() result
    without modifying the underlying model or its model_dump() method.
    """
    if isinstance(data, dict):
        if 'screenshot' in data:
            data.pop('screenshot', None)
        for v in data.values():
            sanitize_agent_result(v)
    elif isinstance(data, list):
        for item in data:
            sanitize_agent_result(item)

app = FastAPI()

# Mount static files directory for serving GIFs
app.mount("/gif_files", StaticFiles(directory="gif_files"), name="gif_files")

# Initialize DB
@app.on_event("startup")
async def startup():
    async with aiosqlite.connect("jobs.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                step_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.commit()

class JobRequest(BaseModel):
    prompt: str
    step_count: int

class JobResponse(BaseModel):
    job_id: str

class JobStatus(BaseModel):
    status: str
    result: Optional[Union[Dict, str]] = None
    message: Optional[str] = None

async def run_agent(job_id: str, prompt: str, step_count: int):
    try:
        # Create the LLM
        llm = ChatOpenAI(model="gpt-4o")

        # Configure browser for headless mode
        browser_config = BrowserConfig(headless=True)
        browser = Browser(config=browser_config)

        # Create the Agent with headless browser
        agent = Agent(
            task=prompt,
            llm=llm,
            browser=browser
        )

        # Create gif_files directory if it doesn't exist
        gif_dir = "gif_files"
        os.makedirs(gif_dir, exist_ok=True)

        # Run the agent
        result = await agent.run(max_steps=step_count)

        # Generate and move GIF file
        gif_filename = f"agent_history_{job_id}.gif"
        agent.create_history_gif(output_path=gif_filename)
        
        # Move GIF to gif_files directory
        gif_source = os.path.join(".", gif_filename)
        gif_target = os.path.join(gif_dir, gif_filename)
        if os.path.exists(gif_source):
            shutil.move(gif_source, gif_target)
        
        # Convert result to JSON using model_dump() and sanitize it
        if result:
            final_result = result.model_dump()
            sanitize_agent_result(final_result)
        else:
            final_result = {"message": "No result"}

        # Mark job complete in DB with JSON string
        async with aiosqlite.connect("jobs.db") as db:
            await db.execute("""
                UPDATE jobs
                SET status = ?, result = ?, updated_at = datetime('now')
                WHERE job_id = ?
            """, ("complete", json.dumps(final_result), job_id))
            await db.commit()
    except Exception as e:
        # Handle any errors during agent execution
        error_message = str(e)
        async with aiosqlite.connect("jobs.db") as db:
            await db.execute("""
                UPDATE jobs
                SET status = ?, result = ?, updated_at = datetime('now')
                WHERE job_id = ?
            """, ("failed", error_message, job_id))
            await db.commit()

@app.post("/jobs", response_model=JobResponse)
async def create_job(request: JobRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    
    # Persist the job as "running"
    async with aiosqlite.connect("jobs.db") as db:
        await db.execute("""
            INSERT INTO jobs(
                job_id, prompt, step_count, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
        """, (job_id, request.prompt, request.step_count, "running"))
        await db.commit()

    # Schedule agent run in the background
    background_tasks.add_task(run_agent, job_id, request.prompt, request.step_count)
    
    return JobResponse(job_id=job_id)

@app.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str):
    async with aiosqlite.connect("jobs.db") as db:
        cursor = await db.execute(
            "SELECT status, result FROM jobs WHERE job_id = ?",
            (job_id,)
        )
        row = await cursor.fetchone()

    if not row:
        return JobStatus(
            status="not_found",
            message="Job not found"
        )

    status, result = row
    if status == "complete":
        return JobStatus(
            status=status,
            result=json.loads(result) if result else None
        )
    elif status == "failed":
        return JobStatus(
            status=status,
            result=result,
            message="Agent execution failed"
        )
    else:
        return JobStatus(
            status=status,
            message="Agent is still running"
        )

@app.get("/jobs/{job_id}/history_gif")
async def get_history_gif(job_id: str):
    gif_filename = f"agent_history_{job_id}.gif"
    gif_path = os.path.join("gif_files", gif_filename)
    
    if not os.path.exists(gif_path):
        raise HTTPException(status_code=404, detail="History GIF not found")
    
    return FileResponse(path=gif_path, media_type="image/gif")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
