import requests
import os

def ask_loop():
    url = "http://localhost:3000/ask"
    user_id = "te_21"
    
    print("=== RAG CHAT CLI (Ketik 'exit' untuk berhenti) ===")
    
    while True:
        question = input("\nPertanyaan Anda: ")
        
        if question.lower() in ['exit', 'quit', 'keluar']:
            break
            
        payload = {"question": question, "user_id": user_id}
        
        try:
            response = requests.post(url, json=payload)
            data = response.json()
            
            print("-" * 30)
            # Menampilkan hasil dengan rapi mirip Format-List
            for key, value in data.items():
                print(f"{key:<15}: {value}")
            print("-" * 30)
            
        except Exception as e:
            print(f"Terjadi kesalahan: {e}")

if __name__ == "__main__":
    ask_loop()
