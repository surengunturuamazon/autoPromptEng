from tqdm import tqdm
from tenacity import retry, stop_after_attempt, wait_exponential
from langchain.llms.bedrock import Bedrock
import re
import json
import boto3
import pandas as pd

import sys
sys.path.append("/home/ec2-user/SageMaker/Auto-Prompt")

from src.utils.configs import REGION, CANDIDATE_MODEL_TEMPERATURE, \
                              system_gen_system_prompt, N_RETRIES, \
                              GENERATION_MODEL_MAX_TOKENS, \
                              GENERATION_MODEL, \
                              GENERATION_MODEL_TEMPERATURE, \
                              RANKING_MODEL, RANKING_MODEL_TEMPERATURE, \
                              CANDIDATE_MODEL, system_prompt, K, \
                              ranking_system_prompt
from src.auto_instruct_prompts import PromptStyle, Task

BEDROCK = boto3.client("bedrock-runtime", region_name="us-east-1")


def get_llm(model_id, max_tokens=1000, temperature=0):
    """
    Initialize llm model
    Args:
        model_id(str): anthropic claude or any other model id
        max_tokens(int): llm max tokens outputted
        temperature(int): temp hyperparam for llm
    Return:
        Bedrock: langchain bedrock instance
    """
    llm = Bedrock(region_name=REGION,
                  model_id=model_id, credentials_profile_name="default",

                  model_kwargs={'max_tokens_to_sample': max_tokens,
                                'temperature': temperature,
                                'stop_sequences': ['Question']})
    llm.client = BEDROCK
    return llm


def llm_response_to_json(llm_output):
    """
    Convert llm response to a json type
    Args:
        llm_output(str): llm response
    Returns:
        dict: llm output in json
    """
    # Use regular expressions to find content between curly braces
    matches = re.findall(r'\[(.*?)\]', llm_output, re.DOTALL)
    if len(matches) < 1:
        return "", []
    try:
        json_obj = json.loads("["+matches[0]+"]")
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {str(e)}")
        return "", []
    return json_obj


def generate_candidate_prompts(description, test_cases, number_of_prompts,
                               choice=True):
    """
    Generate candidate instructions based on the description and test cases
    Args: 
        description(str): the generic prompt for the task (from task_txt)
        test_cases(list): all prompts for the particular styles
        number_of_prompts(int): number of instructions to generate
        choice(boolean): whether to output in a json format
    Returns: 
        list: list of all generated instructions
    """
    llm = get_llm(CANDIDATE_MODEL, max_tokens=8000,
                  temperature=CANDIDATE_MODEL_TEMPERATURE)
    prompts = []
    if choice: 
        formated_prompt = system_gen_system_prompt.format(description=
                                                          description,
                                                          test_cases=
                                                          test_cases,
                                                          number_of_prompts=
                                                          number_of_prompts)
        response = llm(formated_prompt)
        outputs = llm_response_to_json(response)
        for output in outputs:
            prompts.append(output["prompt"])
    else:
        for test_case in test_cases:
            formated_prompt = system_prompt.format(description=description,
                                                   test_case=
                                                   test_case['prompt'])
            response = llm(formated_prompt)
            # response = statement_parser.parse(response).statements
            prompts.append(response)
    return prompts


def expected_score(r1, r2):
    return 1 / (1 + 10**((r2 - r1) / 400))


def update_elo(r1, r2, score1):
    e1 = expected_score(r1, r2)
    e2 = expected_score(r2, r1)
    return r1 + K * (score1 - e1), r2 + K * ((1 - score1) - e2)


# Get Score - up to N_RETRIES times, waiting exponentially between retries.
@retry(stop=stop_after_attempt(N_RETRIES), wait=wait_exponential(multiplier=1,
                                                                 min=4,
                                                                 max=70))
def get_score(description, test_case, pos1, pos2, ranking_model_name,
              ranking_model_temperature):
    """
    Get the score of the two prompts using the description and test_case
    Args:
        description(str): the generic prompt for the task (from task_txt)
        test_cases(list): all prompts for the particular styles
        pos1(str): instruction generation one
        pos2(str): instruction generation two
        ranking_model_name(str): llm model id
        ranking_model_temperature(float): llm model temp
    """
    llm = get_llm(ranking_model_name, max_tokens=1,
                  temperature=ranking_model_temperature)
    sub_prompt = f"""Task: {description.strip()}  \n
        Prompt: {test_case} \n
        Generation A: {pos1} \n
        Generation B: {pos2} \n
        """
    prompt = """
    \n\nAssistant:
    {ranking_system_prompt}
    \n\nHuman:
    {sub_prompt}
    \n\nAssistant:
    """
    formated_prompt = prompt.format(ranking_system_prompt=ranking_system_prompt,
                                    sub_prompt=sub_prompt, test_case=test_case)
    score = llm(formated_prompt)
    return score.replace(' ', '')


@retry(stop=stop_after_attempt(N_RETRIES),
       wait=wait_exponential(multiplier=1, min=4, max=70))
def get_generation(prompt, test_case):
    """
    Generate prompts/instructions using the generation model from configs.
    Args:
        prompt(str): generic prompt to generate instructions
        test_case(list): specific prompts for different styles
    Return: 
        str: newly generated instruction
    """
    key_to_exclude = "expected_ouptut"
    test_case = {key: value for key,
                 value in test_case.items() if key != key_to_exclude}

    llm = get_llm(GENERATION_MODEL, max_tokens=GENERATION_MODEL_MAX_TOKENS,
                  temperature=GENERATION_MODEL_TEMPERATURE)
    combined_prompt = """
    \n\nAssistant:
    {prompt}
    \n\nHuman:
    {test_case}
    \n\nAssistant:
    """
    formated_prompt = combined_prompt.format(prompt=prompt,
                                             test_case=test_case)
    response = llm(formated_prompt)
    return response


def generate_adjacent_combinations(arr):
    """
    Generate combinations of prompts for testing
    Args:
        arr(list): list of prompts
    Returns:
        list: different combinations of tuples for testing
    """
    adjacent_combinations = []

    for i in range(len(arr) - 1):
        current_pair = (arr[i], arr[i + 1])
        adjacent_combinations.append(current_pair)

    return adjacent_combinations


def test_candidate_prompts(test_cases, description, prompts):
    """
    Among all candidate prompts generated, test using an ELO
        rating to find the best prompt. 
    Args:
        description(str): the generic prompt for the task (from task_txt)
        test_cases(list): all prompts for the particular styles
        prompts(list): generated prompts
    Returns:
        dict: prompts as keys and the rating as values
    """
    # Initialize each prompt with an ELO rating of 1200
    prompt_ratings = {prompt: 1200 for prompt in prompts}
    adjacent_prompt_pairs = generate_adjacent_combinations(prompts)
    print(len(adjacent_prompt_pairs))
    # Calculate total rounds for progress bar
    total_rounds = len(test_cases) * len(adjacent_prompt_pairs)

    # Initialize progress bar
    pbar = tqdm(total=total_rounds, ncols=70)

    # For each pair of prompts
    for prompt1, prompt2 in adjacent_prompt_pairs:
        # For each test case
        for test_case in test_cases:
            # Update progress bar
            pbar.update()

            # Generate outputs for each prompt
            # print(test_case)
            generation1 = get_generation(prompt1, test_case)
            print("Gen 1: ", generation1)
            generation2 = get_generation(prompt2, test_case)
            print("Gen 2: ", generation2)
            print("-"*10)
            # Rank the outputs
            score1 = get_score(description, test_case, generation1,
                               generation2, RANKING_MODEL,
                               RANKING_MODEL_TEMPERATURE)
            score2 = get_score(description, test_case, generation2,
                               generation1, RANKING_MODEL,
                               RANKING_MODEL_TEMPERATURE)

            # Convert scores to numeric values
            score1 = 1 if score1 == 'A' else 0 if score1 == 'B' else 0.5
            score2 = 1 if score2 == 'B' else 0 if score2 == 'A' else 0.5

            # Average the scores
            score = (score1 + score2) / 2

            # Update ELO ratings
            r1, r2 = prompt_ratings[prompt1], prompt_ratings[prompt2]
            r1, r2 = update_elo(r1, r2, score)
            prompt_ratings[prompt1], prompt_ratings[prompt2] = r1, r2

            # Print the winner of this round
            if score > 0.5:
                print(f"Winner: {prompt1}")
            elif score < 0.5:
                print(f"Winner: {prompt2}")
            else:
                print("Draw")

    # Close progress bar
    pbar.close()

    return prompt_ratings


def generate_optimal_prompt(description, test_cases, number_of_prompts=10,
                            choice=True):
    """
    Generate the optimal prompt/instruction from the prompt styles
    Args:
        description(str): the generic prompt for the task (from task_txt)
        test_cases(list): all prompts for the particular styles
        number_of_prompts(int): number of prompts to generate per style
        choice(boolean): generate json response if true otherwise don't
    Return:
        pandas Dataframe: final prompt results and ratings
    """
    prompts = generate_candidate_prompts(description, test_cases,
                                         number_of_prompts, choice=choice)
    prompt_ratings = test_candidate_prompts(test_cases, description, prompts)
    # Print the final ELO ratingsz
    table = pd.DataFrame({"Prompt": [], "Rating": []})
    for prompt, rating in sorted(prompt_ratings.items(),
                                 key=lambda item: item[1], reverse=True):
        table.loc[len(table)] = [prompt, rating]
    return table


def generate_ex_prompt_styles(files, question, context, answer,
                              task_txt="ragas_test_task.txt"):
    """
    Get example prompt styles, for example, step by step, 
        with an example, etc..
    Args:
        files(list): list of prompt styles
        question(str): user question
        context(str): context retrieved by RAG
        answer(str): the QA bot llm answer
    Return:
        str: the generic prompt for the task (from task_txt)
        list: all prompts for the particular styles
    """
    # read prompts
    prompts = []
    for fi in files:
        prompt = PromptStyle(fi)
        prompts += [prompt]
    task = Task(f'tasks/{task_txt}')
    task.add_styles(prompts)
    task.prompt = task.prompt.format(question=question, context=context,
                                     answer=answer)
    description = task.prompt
    test_cases = []
    for p in task.all_prompts:
        test_cases += [{'prompt': p.prompt}]
    return description, test_cases