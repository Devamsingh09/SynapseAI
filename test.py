from langchain_groq import ChatGroq
from dotenv import load_dotenv
load_dotenv(override=True)
import os

os.environ["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY")
llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0.1,api_key=os.getenv("GROQ_API_KEY"))
response = llm.invoke("Say hello!")
print(response.content)