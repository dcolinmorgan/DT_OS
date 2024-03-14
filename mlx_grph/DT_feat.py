# conda create -n DT ipykernel 
# python -m ipykernel install --user --name DT
# pip install torch bs4 transformers spacy numpy pandas scikit-learn scipy nltk
from bs4 import BeautifulSoup
import argparse
from tqdm import tqdm
from datetime import datetime
import numpy as np
import concurrent.futures
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.feature_extraction.text import CountVectorizer
from transformers import AutoModel, AutoTokenizer
import torch, spacy,nltk,subprocess, json, requests,string,csv,logging
# nltk.download('punkt')  # run once

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Setup argument parser
parser = argparse.ArgumentParser(description='Process OS data for dynamic features.')
parser.add_argument('-n', type=int, default=10, help='Number of data items to get')
parser.add_argument('-f', type=int, default=3, help='Number of features per item to get')
parser.add_argument('-o', type=str, default='OS_feats.csv', help='Output file name')
parser.add_argument('-s', type=int, default=1, help='Parallelize requests')
args, unknown = parser.parse_known_args()

# Load models and tokenizers
model_name = "distilroberta-base"
model = AutoModel.from_pretrained(model_name)
tokenizer = AutoTokenizer.from_pretrained(model_name)
# !python -m spacy download en_core_web_sm
nlp = spacy.load('en_core_web_sm')

# Define constants
n_gram_range = (1, 2)
stop_words = "english"
embeddings=[]
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model.to(device)

# Define functions
def chunk_text(text, max_len):
    tokens = nltk.word_tokenize(text)
    num_chunks = len(tokens) // max_len
    final_chunk_size = len(tokens) % max_len
    # If the final chunk is too small, distribute its tokens among the other chunks
    if final_chunk_size < max_len / 2:
        num_chunks += 1
        chunk_sizes = [len(tokens) // num_chunks + (1 if i < len(tokens) % num_chunks else 0) for i in range(num_chunks)]
        chunks = [tokens[sum(chunk_sizes[:i]):sum(chunk_sizes[:i+1])] for i in range(num_chunks)]
    else:
        chunks = [tokens[i:i + max_len] for i in range(0, len(tokens), max_len)]
    return chunks


def featurize_stories(text, top_k, max_len):
    # Extract candidate words/phrases
    count = CountVectorizer(ngram_range=n_gram_range, stop_words=stop_words).fit([text])
    all_candidates = count.get_feature_names_out()
    doc = nlp(text)
    noun_phrases = set(chunk.text.strip().lower() for chunk in doc.noun_chunks)
    nouns = set()
    datetimes = set()
    for token in doc:
        if token.pos_ == "NOUN":
            nouns.add(token.text)
        if token.ent_type_ == "DATE":
            datetimes.add(token.text)  # no need to tokenize dates, just add to feature list artificially by repeating
    all_nouns = nouns.union(noun_phrases)
    candidates = list(filter(lambda candidate: candidate in all_nouns, all_candidates))
    candidate_tokens = tokenizer(candidates, padding=True, return_tensors="pt")
    # candidate_tokens = {k: v.to(device) for k, v in (candidate_tokens).items()}
    candidate_embeddings = model(**candidate_tokens)["pooler_output"]
    candidate_embeddings = candidate_embeddings.detach()#.to_numpy()
    # words = nltk.word_tokenize(text)
    # chunks = [words[i:i + 512] for i in range(0, len(words), 512)]
    chunks = chunk_text(text, max_len)  # use this to chunk better and use less padding thus less memory but also less affect from averging
    for chunk in chunks:
        text_tokens = tokenizer(chunk, padding=True, return_tensors="pt")
        # text_tokens = {k: v.to(device) for k, v in (text_tokens).items()}
        text_embedding = model(**text_tokens)["pooler_output"]
        text_embedding = text_embedding.detach()#.to_numpy()
        embeddings.append(text_embedding)
    max_emb_shape = max(embedding.shape[0] for embedding in embeddings)
    padded_embeddings = [np.pad(embedding.cpu(), ((0, max_emb_shape - embedding.shape[0]), (0, 0))) for embedding in embeddings]
    avg_embedding = np.min(padded_embeddings, axis=0)
    distances = cosine_similarity(avg_embedding, candidate_embeddings.cpu())
    torch.cuda.empty_cache()
    return [candidates[index] for index in distances.argsort()[0][::-1][-top_k:]]


def get_data(n=args.n):
    bash_command = f"""
    curl -X GET "https://louie_armstrong:peach-Jam-42-prt@search-opensearch-dev-domain-7grknmmmm7nikv5vwklw7r4pqq.us-east-1.es.amazonaws.com/emergency-management-news/_search" -H 'Content-Type: application/json' -d '{{
        "_source": ["metadata.GDELT_DATE", "metadata.page_title", "metadata.DocumentIdentifier", "metadata.Locations", "metadata.Extras"],
        "size": {n},
        "query": {{
        "match_all": {{}}
        }}
    }}'
    """
    process = subprocess.run(bash_command, shell=True, capture_output=True, text=True)
    output = process.stdout
    data = json.loads(output)
    return data


def process_hit(hit):
    text = []
    source = hit['_source']
    date = datetime.strptime(source['metadata']['GDELT_DATE'], "%Y%m%d%H%M%S")
    date = formatted_date = date.strftime("%d-%m-%Y")
    loc = source['metadata']['Locations']
    loc = loc.replace("'", '"')  # json requires double quotes for keys and string values
    try:
        list_of_dicts = json.loads(loc)
        location_full_names = [dict['Location FullName'] for dict in list_of_dicts if 'Location FullName' in dict]
        loc = location_full_names[0]
    except:
        loc = None
    ex = source['metadata']['Extras']
    title = source['metadata']['page_title']
    url = source['metadata']['DocumentIdentifier']
    try:
        response = requests.get(url)
    except requests.exceptions.ConnectionError:  #
        print(f"timeout for {url}")
        return text,date,loc,title
    if response.status_code != 200:
        print(f"Failed to get {url}")
        return text,date,loc,title
    soup = BeautifulSoup(response.text, 'html.parser')
    paragraphs = soup.find_all(['p'])
    if not paragraphs:
        print(f"No <p> tags in {url}")
        return text,date,loc,title
    for p in paragraphs:
        text.append(p.get_text())
    return text,date,loc,title


def process_data(data,fast=args.s):
    articles = []
    results=[]
    hits = data['hits']['hits']
    if fast==0:
        for hit in hits:
            results.append(process_hit(hit))
    else:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = list(executor.map(process_hit, hits))
    with open('output.csv', 'w') as file:
        writer = csv.writer(file)
        for text,date,loc,title in results:
            articles.append([loc,date,text])
            writer.writerow([loc,date,text]) # force location into top feature, also assume title has important info
            writer.writerow(['\n'])
    return articles


# Main pipeline
def main(args):
    data = get_data(args.n)
    articles = process_data(data)
    rank_articles=[]
    for i in tqdm(articles):
        parts=str(i).split('[', 3)
        try:
            cc=featurize_stories(str(i), top_k = args.f, max_len=512)
            rank_articles.append([parts[1],cc])
        except Exception as e:
            logging.error(f"Failed to process article: {e}")
    with open(args.o, 'w', newline='') as file:
        writer = csv.writer(file)
        writer.writerows(rank_articles)

if __name__ == "__main__":
    main(args)
