from fastapi import FastAPI, BackgroundTasks
import uuid
import asyncio
import aiosqlite
from datetime import datetime
from pydantic import BaseModel
from typing import Optional, Dict, Any

from browser_use.agent.service import Agent
from langchain_openai import ChatOpenAI

app = FastAPI()

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
    result: Optional[str] = None
    message: Optional[str] = None

async def run_agent(job_id: str, prompt: str, step_count: int):
    try:
        # Create the LLM
        llm = ChatOpenAI(model="gpt-4o")

        # Create the Agent
        agent = Agent(
            task=prompt,
            llm=llm
        )

        # Run the agent
        result = await agent.run(max_steps=step_count)
        
        # Convert result to string if it's not already
        final_result = str(result) if result else "No result"

        # Mark job complete in DB
        async with aiosqlite.connect("jobs.db") as db:
            await db.execute("""
                UPDATE jobs
                SET status = ?, result = ?, updated_at = datetime('now')
                WHERE job_id = ?
            """, ("complete", final_result, job_id))
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
            result=result
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
