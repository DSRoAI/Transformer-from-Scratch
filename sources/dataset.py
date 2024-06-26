import random
import torch
import wandb
import logging
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.sampler import Sampler
from torch.nn.utils.rnn import pad_sequence
from datasets import load_dataset
from tokenizers import Tokenizer
from .tokenizer import get_tokenizer_wordlevel, get_tokenizer_bpe

# Configure log
log = logging.getLogger(__name__)
logging.basicConfig(level = logging.INFO) 

# Function to load and preprocess the dataset
def get_dataset(example_cnt, split = 'train'):
    # Load the dataset, shuffle it, and select a subset of examples
    dataset = load_dataset('mt_eng_vietnamese',  "iwslt2015-en-vi", split = split).shuffle(seed = 42)
    dataset = dataset.select(range(example_cnt))
    # Flatten the dataset and rename the columns
    dataset = dataset.flatten()
    dataset = dataset.rename_column('translation.en', 'translation_src')
    dataset = dataset.rename_column('translation.vi', 'translation_trg')
    return dataset

# Function to preprocess the data
def preprocess_data(data, tokenizer, max_len, test_proportion):
    '''
    Including tokenization, filtering by sequence length, splitting, and sorting
    '''
    # Tokenize the data
    def tokenize(example):
        return {
            'translation_src': tokenizer.encode(example['translation_src']).ids,
            'translation_trg': tokenizer.encode(example['translation_trg']).ids,
        }
    data = data.map(tokenize)

    # Compute the length of each sequence
    def sequence_length(example):
        return {
            'length_src': [len(item) for item in example['translation_src']],
            'length_trg': [len(item) for item in example['translation_trg']],
        }      
    data = data.map(sequence_length, batched = True, batch_size = 10000)

    # Filter out sequences that are too long
    def filter_long_sentences(example):
        return example['length_src'] <= max_len and example['length_trg'] <= max_len
    data = data.filter(filter_long_sentences)

    # Split the data into training and testing sets
    data = data.train_test_split(test_size = test_proportion)

    # Sort each split by sequence length for dynamic batching
    data['train'] = data['train'].sort('length_src', reverse = True)
    data['test'] = data['test'].sort('length_src', reverse = True)

    return data

# Custom batch sampler for chunking indices into batches
class CustomBatchSampler(Sampler):
    '''
    Chunks indices into batches of indices for sampling
    '''
    def __init__(self, dataset, batch_size):
        # Dataset is already sorted so just chunk indices into batches of indices for sampling
        self.batch_size = batch_size
        self.indices = range(len(dataset))
        self.batch_of_indices = list(self.chunk(self.indices, self.batch_size))
        self.batch_of_indices = [batch.tolist() for batch in self.batch_of_indices]
    
    # Function to chunk indices into batches
    def chunk(self, indices, chunk_size):
        return torch.split(torch.tensor(indices), chunk_size)
    
    # Function to shuffle and return the batches of indices
    def __iter__(self):
        random.shuffle(self.batch_of_indices)
        return iter(self.batch_of_indices)
    
    # Function to return the number of batches
    def __len__(self):
        return len(self.batch_of_indices)

# PyTorch dataset for the translation task
class TranslationDataset(Dataset):
    '''
    Formats output of preprocess_data into a PyTorch dataset
    '''
    def __init__(self, dataset):
        self.dataset = dataset

    # Function to return the size of the dataset
    def __len__(self):
        return len(self.dataset)

    # Function to return an item from the dataset
    def __getitem__(self, idx):
        src_encoded = self.dataset[idx]['translation_src']
        trg_encoded = self.dataset[idx]['translation_trg']
        
        return (
            torch.tensor(src_encoded),
            torch.tensor(trg_encoded),
        )

# Function to get the dataloaders for the translation task
def get_translation_dataloaders(
    dataset_size,
    vocab_size,
    tokenizer_type,
    tokenizer_save_pth,
    test_proportion,
    batch_size,
    max_len,
    ):

    # Load and preprocess the dataset
    data = get_dataset(dataset_size)
    
    # Get the appropriate tokenizer
    if tokenizer_type == 'wordlevel':
        tokenizer = get_tokenizer_wordlevel(data, vocab_size)
    elif tokenizer_type == 'bpe':
        tokenizer = get_tokenizer_bpe(data, vocab_size)
    
    # Load a pretrained tokenizer if specified, otherwise save the current tokenizer
    if wandb.config.PRETRAIN_TOKENIZER_PT is not None:
        tokenizer = Tokenizer.from_file(wandb.config.PRETRAIN_TOKENIZER_PT)
    else:
        print(f'Saving tokenizer to {tokenizer_save_pth}')
        tokenizer.save(tokenizer_save_pth)
    
    # Preprocess the data
    data = preprocess_data(data, tokenizer, max_len, test_proportion)

    # Create PyTorch datasets
    train_ds = TranslationDataset(data['train'])
    val_ds = TranslationDataset(data['test'])

    # Create custom batch samplers
    custom_batcher_train = CustomBatchSampler(train_ds, batch_size)
    custom_batcher_val= CustomBatchSampler(val_ds, batch_size)
    
    # Function to pad sequences in a batch
    def pad_collate_fn(batch):
        src_sentences, trg_sentences = [], []
        for sample in batch:
            src_sentences += [sample[0]]
            trg_sentences += [sample[1]]

        src_sentences = pad_sequence(src_sentences, batch_first = True, padding_value = 0)
        trg_sentences = pad_sequence(trg_sentences, batch_first = True, padding_value = 0)

        return src_sentences, trg_sentences

    # Create PyTorch dataloaders
    train_dl = DataLoader(train_ds, collate_fn = pad_collate_fn, batch_sampler = custom_batcher_train, pin_memory = True)
    val_dl = DataLoader(val_ds, collate_fn = pad_collate_fn, batch_sampler = custom_batcher_val, pin_memory = True)

    return train_dl, val_dl