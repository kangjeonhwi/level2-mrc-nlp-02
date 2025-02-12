import json
import os
import pickle
import time
import random
from contextlib import contextmanager
from typing import List, NoReturn, Optional, Tuple, Union


import numpy as np
import pandas as pd
from datasets import Dataset, concatenate_datasets, load_from_disk
from tqdm.auto import tqdm

import argparse
from transformers import AutoTokenizer, TrainingArguments

# 커스텀
from Retriever.chw.embedding.sparse import SparseRetrieval
from Retriever.chw.embedding.dense import DenseRetrieval
from Retriever.chw.model.encoder import BertEncoder
from Retriever.chw.embedding.hybrid import HybridLogisticRetrieval
from Retriever.chw.utils.util import MRR, TopkHit

seed = 2024
random.seed(seed)  # python random seed 고정
np.random.seed(seed)  # numpy random seed 고정


@contextmanager
def timer(name):
    t0 = time.time()
    yield
    print(f"[{name}] done in {time.time() - t0:.3f} s")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="")
    parser.add_argument("--dataset_name", metavar="../../data/train_dataset", type=str, help="")
    parser.add_argument(
        "--model_name_or_path",
        metavar="bert-base-multilingual-cased",
        type=str,
        help="",
    )
    parser.add_argument("--data_path", metavar="../../data", type=str, help="")
    parser.add_argument("--context_path", metavar="wikipedia_documents", type=str, help="")
    parser.add_argument("--topk", metavar=3, type=int, help="")
    parser.add_argument("--dense_method", metavar="bert", type=str, help="dense embedding 모델을 인수로 전달합니다. ex) bert")
    parser.add_argument("--device", metavar="cuda", type=str, help="device를 인수로 전달합니다. ex) cuda, cpu")
    parser.add_argument("--mode", metavar="train", type=str, help="실행 방법을 인수로 전달합니다.(새로 훈련 - train, 이미 생성한 모델 및 임베딩 활용 - eval) ex) train, eval")
    parser.add_argument("--sparse_method", metavar="tfidf", type=str, help="sparse 임베딩 방법을 인수로 전달합니다. ex) tfidf, bm25")
    args = parser.parse_args()

    # Test sparse
    org_dataset = load_from_disk(args.dataset_name)
    ## 현재 validation 데이터셋에 대한 테스트, train 데이터셋도 포함시키고 싶을 경우 밑의 org_dataset["train"].flatten_indices() 주석 해제

    full_ds = concatenate_datasets(
        [
            # org_dataset["train"].flatten_indices(),
            org_dataset["validation"].flatten_indices(),
        ]
    )  # train dev 를 합친 4192 개 질문에 대해 모두 테스트
    print("*" * 40, "query dataset", "*" * 40)
    print(full_ds[:2])
    print("dataset size", len(full_ds))
    print("dataset type", type(full_ds))

    ## full_ds의 컬럼 ['title','context','question','id','answers - ['answer_start', 'text']','document_id']
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name_or_path,
        use_fast=False,
    )

    train_dataset = org_dataset

    training_args = TrainingArguments(
        output_dir="dense_retrieval", evaluation_strategy="epoch", learning_rate=3e-5, per_device_train_batch_size=1, per_device_eval_batch_size=2, num_train_epochs=5, weight_decay=0.01
    )
    model_checkpoint = args.model_name_or_path

    # 혹시 위에서 사용한 encoder가 있다면 주석처리 후 진행해주세요 (CUDA ...)
    tokenizer = AutoTokenizer.from_pretrained(model_checkpoint)
    p_encoder = BertEncoder.from_pretrained(model_checkpoint).to(args.device)
    q_encoder = BertEncoder.from_pretrained(model_checkpoint).to(args.device)

    sparse_retriever = SparseRetrieval(
        tokenize_fn=tokenizer.tokenize,
        data_path=args.data_path,
        context_path=args.context_path,
    )

    if args.sparse_method == "bm25":
        sparse_retriever.get_bm25_embedding()
    else:
        sparse_retriever.get_sparse_embedding()

    dense_retriever = DenseRetrieval(
        args=training_args, dataset=org_dataset["train"], data_path=args.data_path, context_path=args.context_path, num_neg=2, tokenizer=tokenizer, p_encoder=p_encoder, q_encoder=q_encoder
    )

    #### 실행하고자 하는 모드에 따라서 train 혹은 load_model 하나를 선택하여 실행 (train : 학습 후 인코더 활용, load_model : 기학습된 모델을 불러온 후 인코더 활용)
    if args.mode == "train":
        dense_retriever.train()
    else:
        dense_retriever.load_model()

    #### get_passage_embedding()을 실행하면 passage embedding을 실행시키거나 이미 임베딩 파일이 존재할 경우 그 파일을 불러와서 내부 변수에 저장합니다. 이는 retrieve 과정에 활용됩니다.
    #### train mode일 경우 임베딩 파일이 존재하던지 상관 없이 다시 임베딩을 생성합니다.
    dense_retriever.get_passage_embedding(mode=args.mode)

    hybrid_retriever = HybridLogisticRetrieval(args, org_dataset["train"], data_path=args.data_path, context_path=args.context_path, sparse_retriever=sparse_retriever, dense_retriever=dense_retriever)

    labels = [0, 1]
    hybrid_retriever.get_logistic_regression(save_name="hybrid_logistic.bin", labels=labels, topk=args.topk)

    # print("single with no faiss - query scores, indices", scores, indices)
    with timer("bulk query by exhaustive search"):
        results = hybrid_retriever.retrieve(query_or_dataset=full_ds, topk=args.topk)
        # results["correct"] = results.apply(lambda row: row["context"].find(row["original_context"]) != -1, axis=1)

        print("idx < 10 context compare", results[:10]["original_context"], results[:10]["context"])

        print("correct retrieval result by exhaustive search", TopkHit(results))
        print("MRR = ", MRR(results))
