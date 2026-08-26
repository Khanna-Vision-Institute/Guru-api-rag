from openai import OpenAI
client = OpenAI()

vec = client.embeddings.create(
    model="text-embedding-3-large",
    input="lasik"
).data[0].embedding

print(len(vec))
print(vec)
