from langchain_openai import OpenAIEmbeddings
from langchain_openai.chat_models import ChatOpenAI
from langchain.chains.query_constructor.base import AttributeInfo
from langchain.retrievers.self_query.base import SelfQueryRetriever
from langchain_community.vectorstores import Pinecone as LangPine
import re
import csv
import os
import streamlit as st
import neo4j
from openai import OpenAI
from pinecone import Pinecone
from pinecone_text.sparse import BM25Encoder
from typing import List, Dict
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@st.cache_resource
def connect_to_neo4j():
    return neo4j.GraphDatabase.driver(os.getenv("NEO4J_URI"), auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASS")))

@st.cache_resource
def connect_to_pinecone():
    pinecone_api_key = os.getenv('PINECONE_API_KEY')
    pinecone_environment = os.getenv('PINECONE_ENVIRONMENT')
    index_name = 'delfi'
    pc = Pinecone(api_key=pinecone_api_key, environment=pinecone_environment)
    return pc.Index(index_name)

def graphp(pitanje):
    def run_cypher_query(driver, query):
        with driver.session() as session:
            results = session.run(query)
            cleaned_results = []
            max_characters=100000
            total_characters = 0
            max_record_length = 0
            min_record_length = float('inf')
            
            for record in results:
                cleaned_record = {}
                for key, value in record.items():
                    if isinstance(value, neo4j.graph.Node):
                        # Ako je vrednost Node objekat, pristupamo properties atributima
                        properties = {k: v for k, v in value._properties.items()}
                    else:
                        # Ako je vrednost obična vrednost, samo je dodamo
                        properties = {key: value}
                    
                    for prop_key, prop_value in properties.items():
                        # Uklanjamo prefiks 'b.' ako postoji
                        new_key = prop_key.split('.')[-1]
                        cleaned_record[new_key] = prop_value
                
                record_length = sum(len(str(value)) for value in cleaned_record.values())
                if total_characters + record_length > max_characters:
                    break  # Prekida se ako dodavanje ovog zapisa prelazi maksimalan broj karaktera

                cleaned_results.append(cleaned_record)
                record_length = sum(len(str(value)) for value in cleaned_record.values())
                total_characters += record_length
                if record_length > max_record_length:
                    max_record_length = record_length
                if record_length < min_record_length:
                    min_record_length = record_length
        return cleaned_results
        
    def generate_cypher_query(question):
        prompt = f"Translate the following user question into a Cypher query. Use the given structure of the database: {question}"
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            messages=[
                {
            "role": "system",
            "content": (
                "You are a helpful assistant that converts natural language questions into Cypher queries for a Neo4j database."
                "The database has 3 node types: Author, Book, Genre, and 2 relationship types: BELONGS_TO and WROTE."
                "Only Book nodes have properties: id, category, title, price, quantity, pages, and eBook."
                "All node and relationship names are capitalized (e.g., Author, Book, Genre, BELONGS_TO, WROTE)."
                "Genre names are also capitalized (e.g., Drama, Fantastika). Please ensure that the generated Cypher query uses these exact capitalizations."
                "Ensure to include a condition to check that the quantity property of Book nodes is greater than 0 to ensure the books are in stock where this filter is plausable."
                "For recommendations, ensure to return just the title and the author of the recommended books."
                "When writing the Cypher query, ensure that instead of '=' use CONTAINS, in order to return all items which contains the seatched term."
                "When generating the Cypher query, ensure to handle inflected forms properly. For example, if the user asks for books by 'Tolkiena,' generate a query for 'Tolkien' instead, removing any inflections."
                "When returning some properties of books, ensure to return the title too."
                "Ensure that the Cypher query returns only the requested data. If the user does not specify which properties they want to retrieve, return only the title and the author."

                "Here is an example user question and the corresponding Cypher query: "
                "Example user question: 'Pronađi knjigu Da Vinčijev kod.' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Da Vinčijev kod') RETURN b"

                "Example user question: 'Interesuje me knjiga Piramide.' "
                "Cypher query: MATCH (b:Book)-[:WROTE]-(a:Author) WHERE toLower(b.title) CONTAINS toLower('Piramide') AND b.quantity > 0 RETURN b.title AS title, a.name AS author"
                
                "Example user question: 'Preporuci mi knjige slicne knjizi The Hobbit.' "
                "Cypher query: MATCH (b:Book)-[:BELONGS_TO]->(g:Genre) WHERE toLower(b.title) CONTAINS toLower('Krhotine') WITH g MATCH (rec:Book)-[:BELONGS_TO]->(g)<-[:BELONGS_TO]-(b:Book) WHERE b.title CONTAINS 'Krhotine' AND rec.quantity > 0 MATCH (rec)-[:WROTE]-(a:Author) RETURN rec.title AS title, a.name AS author"

                "Example user question: 'Koja je cena za Autostoperski vodič kroz galaksiju?' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Autostoperski vodič kroz galaksiju') AND b.quantity > 0 RETURN b.title AS title, b.price AS price"

                "Example user question: 'Da li imate anu karenjinu na stanju' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Ana Karenjina') AND b.quantity > 0 RETURN b.title AS title"
            )
        },
                {"role": "user", "content": prompt}
            ]
        )
        cypher_query = response.choices[0].message.content.strip()

        # Uklanjanje nepotrebnog teksta oko upita
        if '```cypher' in cypher_query:
            cypher_query = cypher_query.split('```cypher')[1].split('```')[0].strip()

        return cypher_query

    def get_descriptions_from_pinecone(ids):
        # Initialize 
        host = os.getenv("PINECONE_HOST")
        pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"), host=host)
        index = pc.Index(host=host)

        # Fetch the vectors by IDs
        results = index.fetch(ids=ids, namespace="Opisi")
        descriptions = {}

        for id in ids:
            if id in results['vectors']:
                vector_data = results['vectors'][id]
                if 'metadata' in vector_data:
                    descriptions[id] = vector_data['metadata'].get('text', 'No description available')
                else:
                    descriptions[id] = 'Metadata not found in vector data.'
            else:
                descriptions[id] = 'Nemamo opis za ovaj artikal.'
        # print(f"Descriptions: {descriptions}")
        return descriptions

    def combine_data(book_data, descriptions):
        combined_data = []

        for book in book_data:        
            book_id = book.get('id', None)
            
            description = descriptions.get(book_id, 'No description available')
            combined_entry = {**book, 'description': description}
            combined_data.append(combined_entry)
        return combined_data

    def display_results(combined_data):
        output = " "
        for data in combined_data:
            if 'title' in data:
                output += f"Title: {data['title']}\n"
            if 'category' in data:
                output += f"Category: {data['category']}\n"
            if 'price' in data:
                output += f"Price: {data['price']}\n"
            if 'quantity' in data:
                output += f"Quantity: {data['quantity']}\n"
            if 'pages' in data:
                output += f"Pages: {data['pages']}\n"
            if 'eBook' in data:
                output += f"eBook: {data['eBook']}\n"
            if 'description' in data:
                output += f"Description: {data['description']}\n\n"

    def is_valid_cypher(cypher_query):
        # Provera validnosti Cypher upita (osnovna provera)
        if not cypher_query or "MATCH" not in cypher_query.upper():
            return False
        return True

    def has_id_field(data):
        # Provera da li vraćeni podaci sadrže 'id' polje
        return all('id' in item for item in data)

    def formulate_answer_with_llm(question, graph_data):
        input_text = f"Pitanje: '{question}'\nPodaci iz grafa: {graph_data}\nMolimo formulišite odgovor na osnovu ovih podataka."
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that formulates answers based on given data. You have been provided with a user question and data returned from a graph database. Please formulate an answer based on these inputs."},
                {"role": "user", "content": input_text}
            ]
        )
        return response.choices[0].message.content.strip()
 
    driver = connect_to_neo4j()
    
    while True:
        cypher_query = generate_cypher_query(pitanje)
        if is_valid_cypher(cypher_query):
            try:
                book_data = run_cypher_query(driver, cypher_query)
                print(has_id_field(book_data))

                # Define the regex pattern to match both 'id' and 'b.id'
                pattern = r"'(?:b\.)?id': '([^']+)'"
                
                # Extract the IDs from the Cypher query result
                book_ids = []
                try:
                    for data in book_data:
                        match = re.search(pattern, str(data))
                        if match:
                            book_ids.append(match.group(1))
                except Exception as e:
                    print(f"An error occurred: {e}")
                
                if not book_ids:
                    return formulate_answer_with_llm(pitanje, book_data)

                # book_ids = [book['id'] for book in book_data]
                descriptionsDict = get_descriptions_from_pinecone(book_ids)
                # descriptions = list(descriptionsDict.values())
                combined_data = combine_data(book_data, descriptionsDict)
                display_results(combined_data)
                return
            except Exception as e:
                print(f"Greška pri izvršavanju upita: {e}. Molimo pokušajte ponovo.")
        else:
            print("Traženi pojam nije jasan. Molimo pokušajte ponovo.")



def pineg(pitanje):
    namespace = 'opisi'
    index = connect_to_pinecone()

    def run_cypher_query(id):
        driver = connect_to_neo4j()
        query = f"MATCH (b:Book) WHERE b.id = '{id}' RETURN b"
        with driver.session() as session:
            result = session.run(query)
            book_data = []
            for record in result:
                book_node = record['b']
                book_data.append({
                    'id': book_node['id'],
                    'title': book_node['title'],
                    'category': book_node['category'],
                    'price': book_node['price'],
                    'quantity': book_node['quantity'],
                    'pages': book_node['pages'],
                    'eBook': book_node['eBook']
                })
            return book_data

    def get_embedding(text, model="text-embedding-3-large"):
        response = client.embeddings.create(
            input=[text],
            model=model
        ).data[0].embedding
        # print(f"Embedding Response: {response}")
        
        return response

    def dense_query(query, top_k=5, filter=None, namespace=namespace):
        # Get embedding for the query
        dense = get_embedding(text=query)
        # print(f"Dense: {dense}")

        query_params = {
            'top_k': top_k,
            'vector': dense,
            'include_metadata': True,
            'namespace': namespace
        }

        response = index.query(**query_params)
        # print(f"Response: {response}")

        matches = response.to_dict().get('matches', [])
        # print(f"Matches: {matches}")

        return matches

    def search_pinecone(query: str, top_k: int = 5) -> List[Dict]:
        # Dobij embedding za query
        query_embedding = dense_query(query)
        # print(f"Results: {query_embedding}")

        # Ekstraktuj id i text iz metapodataka rezultata
        matches = []
        for match in query_embedding:
            metadata = match['metadata']
            matches.append({
                'id': metadata['id'],
                'text': metadata['text']
            })
        
        # print(f"Matches: {matches}")
        return matches

    def combine_data(book_data, descriptions):
        combined_data = []
        for book in book_data:
            combined_entry = {**book, 'description': descriptions}
            combined_data.append(combined_entry)
            print(f"Combined Entry: {combined_entry}")
        return combined_data

    query = pitanje.strip()
    search_results = search_pinecone(query)

    combined_results = []

    for result in search_results:
        
        additional_data = run_cypher_query(result['id'])
        
        # Korak 3: Kombinovanje podataka
        combined_data = combine_data(additional_data, result['text'])
        
        combined_results.append(combined_data)
    
    # return combined_results
    # driver = connect_to_neo4j(uri, user, password)
    
    # query = input("Enter your search content: ")

    # results = search_pinecone(query)
    # print(f"The type of the variable 'query' is: {type(results)}")
    # ids, descriptions = zip(*results)
    
    # if not ids:
    #     print("No matching books found.")
    #     return
    
    # ids_str = ', '.join([f"'{id}'" for id in ids])
    # cypher_query = f"MATCH (b:Book) WHERE b.id IN [{ids_str}] RETURN b"
    # book_data = run_cypher_query(driver, cypher_query)
    
    # combined_data = combine_data(book_data, descriptions)
    output = " "
    for data in combined_data:
        output += "Title: {data['title']}\n\n"
        output += f"Title: {data['title']}\n"
        output += f"Category: {data['category']}\n"
        output += f"Price: {data['price']}\n"
        output += f"Quantity: {data['quantity']}\n"
        output += f"Pages: {data['pages']}\n"
        output += f"eBook: {data['eBook']}\n"
        output += f"Description: {data['description']}\n\n\n"
    return output


def order_search(id_porudzbine):
    match = re.search(r'\d{5,}', id_porudzbine)
    if not match:
        return "No integer found in the prompt."
    
    order_number = int(match.group())

    try:
        with open('orders.csv', mode='r', encoding='utf-8-sig') as file:
            csv_reader = csv.reader(file)
            next(csv_reader)
            for row in csv_reader:
                if int(row[0]) == order_number:
                    return ", ".join(row)
        return f"Order number {order_number} not found in the CSV file."
    except FileNotFoundError:
        return "The file 'orders.csv' does not exist."
    except Exception as e:
        return f"An error occurred: {e}"


def SelfQueryDelfi(upit, api_key=None, environment=None, index_name='delfi', namespace='opisi', openai_api_key=None, host=None):
    """
    Executes a query against a Pinecone vector database using specified parameters or environment variables. 
    The function initializes the Pinecone and OpenAI services, sets up the vector store and metadata, 
    and performs a query using a custom retriever based on the provided input 'upit'.
    
    It is used for self-query on metadata.

    Parameters:
    upit (str): The query input for retrieving relevant documents.
    api_key (str, optional): API key for Pinecone. Defaults to PINECONE_API_KEY from environment variables.
    environment (str, optional): Pinecone environment. Defaults to PINECONE_API_KEY from environment variables.
    index_name (str, optional): Name of the Pinecone index to use. Defaults to 'positive'.
    namespace (str, optional): Namespace for Pinecone index. Defaults to NAMESPACE from environment variables.
    openai_api_key (str, optional): OpenAI API key. Defaults to OPENAI_API_KEY from environment variables.

    Returns:
    str: A string containing the concatenated results from the query, with each document's metadata and content.
         In case of an exception, it returns the exception message.

    Note:
    The function is tailored to a specific use case involving Pinecone and OpenAI services. 
    It requires proper setup of these services and relevant environment variables.
    """
    
    # Use the passed values if available, otherwise default to environment variables
    api_key = api_key if api_key is not None else os.getenv('PINECONE_API_KEY')
    environment = environment if environment is not None else os.getenv('PINECONE_API_KEY')
    # index_name is already defaulted to 'positive'
    namespace = namespace if namespace is not None else os.getenv("NAMESPACE")
    openai_api_key = openai_api_key if openai_api_key is not None else os.getenv("OPENAI_API_KEY")
    host = host if host is not None else os.getenv("PINECONE_HOST")
   
    embeddings = OpenAIEmbeddings(model="text-embedding-3-large")

    # prilagoditi stvanim potrebama metadata
    metadata_field_info = [
        AttributeInfo(name="authors", description="The author(s) of the document", type="string"),
        AttributeInfo(name="category", description="The category of the document", type="string"),
        AttributeInfo(name="chunk", description="The chunk number of the document", type="integer"),
        AttributeInfo(name="date", description="The date of the document", type="string"),
        AttributeInfo(name="eBook", description="Whether the document is an eBook", type="boolean"),
        AttributeInfo(name="genres", description="The genres of the document", type="string"),
        AttributeInfo(name="id", description="The unique ID of the document", type="string"),
        AttributeInfo(name="text", description="The main content of the document", type="string"),
        AttributeInfo(name="title", description="The title of the document", type="string"),
    ]

    # Define document content description
    document_content_description = "Content of the document"

    # Prilagoditi stvanom nazivu namespace-a
    vectorstore = LangPine.from_existing_index(
        index_name, embeddings, "context", namespace=namespace)

    # Initialize OpenAI embeddings and LLM
    llm = ChatOpenAI(model="gpt-4o", temperature=0.0)
    retriever = SelfQueryRetriever.from_llm(
        llm,
        vectorstore,
        document_content_description,
        metadata_field_info,
        enable_limit=True,
        verbose=True,
    )
    try:
        result = ""
        doc_result = retriever.get_relevant_documents(upit)
        for document in doc_result:
            result += "Authors: " + ", ".join(document.metadata['authors']) + "\n"
            result += "Category: " + document.metadata['category'] + "\n"
            result += "Chunk: " + str(document.metadata['chunk']) + "\n"
            result += "Date: " + document.metadata['date'] + "\n"
            result += "eBook: " + str(document.metadata['eBook']) + "\n"
            result += "Genres: " + ", ".join(document.metadata['genres']) + "\n"
            result += "ID: " + document.metadata['id'] + "\n"
            result += "Title: " + document.metadata['title'] + "\n"
            result += "Content: " + document.page_content + "\n\n"
    except Exception as e:
        result = e
    
    return result


class HybridQueryProcessor:
    """
    A processor for executing hybrid queries using Pinecone.

    This class allows the execution of queries that combine dense and sparse vector searches,
    typically used for retrieving and ranking information based on text data.

    Attributes:
        api_key (str): The API key for Pinecone.
        environment (str): The Pinecone environment setting.
        alpha (float): The weight used to balance dense and sparse vector scores.
        score (float): The score treshold.
        index_name (str): The name of the Pinecone index to be used.
        index: The Pinecone index object.
        namespace (str): The namespace to be used for the Pinecone index.
        top_k (int): The number of results to be returned.
            
    Example usage:
    processor = HybridQueryProcessor(api_key=environ["PINECONE_API_KEY"], 
                                 environment=environ["PINECONE_API_KEY"],
                                 alpha=0.7, 
                                 score=0.35,
                                 index_name='custom_index'), 
                                 namespace=environ["NAMESPACE"],
                                 top_k = 10 # all params are optional

    result = processor.hybrid_query("some query text")    
    """

    def __init__(self, **kwargs):
        """
        Initializes the HybridQueryProcessor with optional parameters.

        The API key and environment settings are fetched from the environment variables.
        Optional parameters can be passed to override these settings.

        Args:
            **kwargs: Optional keyword arguments:
                - api_key (str): The API key for Pinecone (default fetched from environment variable).
                - environment (str): The Pinecone environment setting (default fetched from environment variable).
                - alpha (float): Weight for balancing dense and sparse scores (default 0.5).
                - score (float): Weight for balancing dense and sparse scores (default 0.05).
                - index_name (str): Name of the Pinecone index to be used (default 'positive').
                - namespace (str): The namespace to be used for the Pinecone index (default fetched from environment variable).
                - top_k (int): The number of results to be returned (default 6).
        """
        self.api_key = kwargs.get('api_key', os.getenv('PINECONE_API_KEY'))
        self.environment = kwargs.get('environment', os.getenv('PINECONE_API_KEY'))
        self.alpha = kwargs.get('alpha', 0.5)  # Default alpha is 0.5
        self.score = kwargs.get('score', 0.05)  # Default score is 0.05
        self.index_name = kwargs.get('index', 'neo-positive')  # Default index is 'positive'
        self.namespace = kwargs.get('namespace', os.getenv("NAMESPACE"))  
        self.top_k = kwargs.get('top_k', 6)  # Default top_k is 6
        self.index = None
        self.host = "https://neo-positive-a9w1e6k.svc.apw5-4e34-81fa.pinecone.io"
        self.check_namespace = True if self.namespace in ["brosureiuputstva", "servis"] else False
        self.init_pinecone()

    def init_pinecone(self):
        """
        Initializes the Pinecone connection and index.
        """
        pinecone=Pinecone(api_key=self.api_key, host=self.host)
        self.index = pinecone.Index(host=self.host)

    def hybrid_score_norm(self, dense, sparse):
        """
        Normalizes the scores from dense and sparse vectors using the alpha value.

        Args:
            dense (list): The dense vector scores.
            sparse (dict): The sparse vector scores.

        Returns:
            tuple: Normalized dense and sparse vector scores.
        """
        return ([v * self.alpha for v in dense], 
                {"indices": sparse["indices"], 
                 "values": [v * (1 - self.alpha) for v in sparse["values"]]})
    
    def hybrid_query(self, upit, top_k=None, filter=None, namespace=None):
        # Get embedding and unpack results
        dense = self.get_embedding(text=upit)

        # Use those results in another function call
        hdense, hsparse = self.hybrid_score_norm(
            sparse=BM25Encoder().fit([upit]).encode_queries(upit),
            dense=dense
        )

        query_params = {
            'top_k': top_k or self.top_k,
            'vector': hdense,
            'sparse_vector': hsparse,
            'include_metadata': True,
            'namespace': namespace or self.namespace
        }

        if filter:
            query_params['filter'] = filter

        response = self.index.query(**query_params)

        matches = response.to_dict().get('matches', [])

        results = []
        for match in matches:
            metadata = match.get('metadata', {})
            context = metadata.get('context', '')
            chunk = metadata.get('chunk')
            source = metadata.get('source')
            if self.check_namespace:
                filename = metadata.get('filename')
                url = metadata.get('url')
            try:
                score = match.get('score', 0)
            except:
                score = metadata.get('score', 0)
            if context:
                results.append({"page_content": context, "chunk": chunk, "source": source, "score": score})
                if self.check_namespace:
                    results[-1]["filename"] = filename
                    results[-1]["url"] = url
        
        return results
       
    def process_query_results(self, upit, dict=False):
        """
        Processes the query results and prompt tokens based on relevance score and formats them for a chat or dialogue system.
        Additionally, returns a list of scores for items that meet the score threshold.
        """
        tematika = self.hybrid_query(upit)
        if not dict:
            uk_teme = ""
            score_list = []
            for item in tematika:
                if item["score"] > self.score:
                    uk_teme += item["page_content"] + "\n\n"
                    score_list.append(item["score"])
                    if self.check_namespace:
                        uk_teme += f"Filename: {item['filename']}\n"
                        uk_teme += f"URL: {item['url']}\n\n"
            
            return uk_teme, score_list
        else:
            return tematika, []
        
    def get_embedding(self, text, model="text-embedding-3-large"):

        """
        Retrieves the embedding for the given text using the specified model.

        Args:
            text (str): The text to be embedded.
            model (str): The model to be used for embedding. Default is "text-embedding-3-large".

        Returns:
            list: The embedding vector of the given text.
            int: The number of prompt tokens used.
        """
        
        text = text.replace("\n", " ")
        result = client.embeddings.create(input=[text], model=model).data[0].embedding
       
        return result
    
