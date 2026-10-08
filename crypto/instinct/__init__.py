# -*- coding: utf-8 -*-
"""
盘感模拟模块（批次12：RAG + LLM Wiki）
========================================
模拟交易员本人"盘感"的语料→检索→推理→验证闭环。
架构与口径见 doc/RAG_LLM_Wiki模拟盘感落地方案.md。

模块边界（硬约束）：本包只读行情/复盘数据，不 import 任何下单交易模块，
永不接入交易执行链路（影子级起步，达标也只到"参考"级）。

子模块：
  corpus_builder   三源语料标准化入库（P1）
  （后续批次）retriever / wiki_repo / wiki_distiller / llm_gateway /
              predict_service / instinct_routes
"""
