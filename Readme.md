# Automatic Prompt Generation

## Overview
This repository goes over a way to automatically generate instructions. For example, instead of sending a question to an LLM, an instruction will be generated before the question in order to get the most accurate response by the QA bot. The repository is based on the original notebook by Matt Shumer, https://github.com/mshumer/gpt-prompt-engineer. 

## Steps to Run AutoPrompt
1. Run ```pip install -r requirements.txt```

2. Make sure to edit the configs file in src/utils.configs.py, primarily the absolute path variables. 

3. Make sure you run your dataset with Ragas. Upload your dataset (update ragas_output.csv with your dataset) in the data folder. Instructions for how to run Ragas is in the 'Prompt_Engineer_Ragas.ipynb' notebook. 

4. Go through notebook 'notebooks/Prompt_Engineer_Ragas.ipynb'. Uncomment commands under the 'Ragas' section if you need to run ragas evaluation on the dataset. The outputs from the notebook are uploaded in the data folder. 