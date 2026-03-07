import os

# Azure AI Agent
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import ListSortOrder
from azure.identity import AzureCliCredential

from dotenv import load_dotenv
load_dotenv(".env")

AZURE_PROJECT_ENDPOINT = os.getenv("AZURE_PROJECT_ENDPOINT")
AGENT_ID = os.getenv("AGENT_ID")

# === 実行 ===
project = AIProjectClient(
    credential=DefaultAzureCredential(),
    endpoint="https://aifundry-bingsearch.services.ai.azure.com/api/projects/proj-default")

agent = project.agents.get_agent("asst_ZVSbUgcSDY86MKYGE6Vm1Qa0")

thread = project.agents.threads.create()
print(f"Created thread, ID: {thread.id}")

message = project.agents.messages.create(
    thread_id=thread.id,
    role="user",
    content="Hi Agent480"
)

run = project.agents.runs.create_and_process(
    thread_id=thread.id,
    agent_id=agent.id)

if run.status == "failed":
    print(f"Run failed: {run.last_error}")
else:
    messages = project.agents.messages.list(thread_id=thread.id, order=ListSortOrder.ASCENDING)

    for message in messages:
        if message.text_messages:
            print(f"{message.role}: {message.text_messages[-1].text.value}")