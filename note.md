           User Request
                │
                ▼
           Application / Microservice
                │
                ▼
           403 Forbidden
                │
     ├──────────────────────┐
     │                      │
     ▼                      ▼
Application Log       Security Log
(existing log)        (new structured log)
     │                      │
     └──────────┬───────────┘
                │
                ▼
        Existing Log Pipeline
                │
                ▼
           Elasticsearch
                │
                ▼
        
ff      Kibana