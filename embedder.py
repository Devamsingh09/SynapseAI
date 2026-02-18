from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer
from langchain_community.document_loaders import PyPDFLoader
from langchain_huggingface import HuggingFaceEmbeddings

def generate_vectorstore():
    loader = PyPDFLoader("C:\\Users\\devam\\OneDrive\\Desktop\\CAMPUSX_GENAI\\Lang-Graph\\chatbot_with_ui\\Ethics of Data Science- Chapter10.pdf")
    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000,chunk_overlap=200)
    chunks = splitter.split_documents(docs)
    embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
    vector_store = FAISS.from_documents(chunks,embeddings)
    vector_store.save_local("faiss_ethics_ch10")
    return vector_store

if __name__=="__main__":
    generate_vectorstore()