from __future__ import annotations
import datetime
import logging

import tiktoken

import openai

import requests
import json
from datetime import date
from calendar import monthrange

# Models can be found here: https://platform.openai.com/docs/models/overview
GPT_3_MODELS = ("gpt-3.5-turbo", "gpt-3.5-turbo-0301")
GPT_4_MODELS = ("gpt-4", "gpt-4-0314")
GPT_4_32K_MODELS = ("gpt-4-32k", "gpt-4-32k-0314")
GPT_ALL_MODELS = GPT_3_MODELS + GPT_4_MODELS + GPT_4_32K_MODELS


def default_max_tokens(model: str) -> int:
    """
    Получает количество максимальных лексем по умолчанию для данной модели.
    :param model: Имя модели
    :return: Число максимальных лексем по умолчанию
    """
    return 1200 if model in GPT_3_MODELS else 2400


class OpenAIHelper:
    """
    Вспомогательный класс ChatGPT.
    """

    def __init__(self, config: dict):
        """
        Инициализирует класс-помощник OpenAI с заданной конфигурацией.
        :param config: Словарь, содержащий конфигурацию GPT
        """
        openai.api_key = config['api_key']
        openai.proxy = config['proxy']
        self.config = config
        self.conversations: dict[int: list] = {}  # {chat_id: history}
        self.last_updated: dict[int: datetime] = {}  # {chat_id: last_update_timestamp}

    def get_conversation_stats(self, chat_id: int) -> tuple[int, int]:
        """
        Получает количество сообщений и токенов, использованных в разговоре.
        :param chat_id: ID чата
        :return: Кортеж, содержащий количество использованных сообщений и токенов.
        """
        if chat_id not in self.conversations:
            self.reset_chat_history(chat_id)
        return len(self.conversations[chat_id]), self.__count_tokens(self.conversations[chat_id])

    async def get_chat_response(self, chat_id: int, query: str) -> tuple[str, str]:
        """
        Получает полный ответ от модели GPT.
        :param chat_id: ID чата
        :param query: Запрос, который нужно отправить в модель
        :return: Ответ от модели и количество использованных токенов
        """
        response = await self.__common_get_chat_response(chat_id, query)
        answer = ''

        if len(response.choices) > 1 and self.config['n_choices'] > 1:
            for index, choice in enumerate(response.choices):
                content = choice['message']['content'].strip()
                if index == 0:
                    self.__add_to_history(chat_id, role="assistant", content=content)
                answer += f'{index + 1}\u20e3\n'
                answer += content
                answer += '\n\n'
        else:
            answer = response.choices[0]['message']['content'].strip()
            self.__add_to_history(chat_id, role="assistant", content=answer)

        if self.config['show_usage']:
            answer += "\n\n---\n" \
                      f"💰 Tokens used: {str(response.usage['total_tokens'])}" \
                      f" ({str(response.usage['prompt_tokens'])} prompt," \
                      f" {str(response.usage['completion_tokens'])} completion)"

        return answer, response.usage['total_tokens']

    async def get_chat_response_stream(self, chat_id: int, query: str):
        """
        Потоковый ответ от модели GPT.
        :param chat_id: ID чата
        :param query: Запрос, который нужно отправить в модель
        :return: Ответ от модели и количество использованных токенов, или 'not_finished'.
        """
        response = await self.__common_get_chat_response(chat_id, query, stream=True)

        answer = ''
        async for item in response:
            if 'choices' not in item or len(item.choices) == 0:
                continue
            delta = item.choices[0].delta
            if 'content' in delta:
                answer += delta.content
                yield answer, 'not_finished'
        answer = answer.strip()
        self.__add_to_history(chat_id, role="assistant", content=answer)
        tokens_used = str(self.__count_tokens(self.conversations[chat_id]))

        if self.config['show_usage']:
            answer += f"\n\n---\n💰 Используемые токены: {tokens_used}"

        yield answer, tokens_used

    async def __common_get_chat_response(self, chat_id: int, query: str, stream=False):
        """
        Запрос ответа от модели GPT.
        :param chat_id: ID чата
        :param query: Запрос, который нужно отправить в модель
        :return: Ответ от модели и количество использованных токенов
        """
        try:
            if chat_id not in self.conversations or self.__max_age_reached(chat_id):
                self.reset_chat_history(chat_id)

            self.last_updated[chat_id] = datetime.datetime.now()

            self.__add_to_history(chat_id, role="user", content=query)

            # Summarize the chat history if it's too long to avoid excessive token usage
            token_count = self.__count_tokens(self.conversations[chat_id])
            exceeded_max_tokens = token_count + self.config['max_tokens'] > self.__max_model_tokens()
            exceeded_max_history_size = len(self.conversations[chat_id]) > self.config['max_history_size']

            if exceeded_max_tokens or exceeded_max_history_size:
                logging.info(f'История чата для чата ID {chat_id} слишком длинный. Подведение итогов...')
                try:
                    summary = await self.__summarise(self.conversations[chat_id][:-1])
                    logging.debug(f'Summary: {summary}')
                    self.reset_chat_history(chat_id)
                    self.__add_to_history(chat_id, role="assistant", content=summary)
                    self.__add_to_history(chat_id, role="user", content=query)
                except Exception as e:
                    logging.warning(f'Ошибка при подведении итогов истории чатов: {str(e)}. Выскакивающие элементы вместо...')
                    self.conversations[chat_id] = self.conversations[chat_id][-self.config['max_history_size']:]

            return await openai.ChatCompletion.acreate(
                model=self.config['model'],
                messages=self.conversations[chat_id],
                temperature=self.config['temperature'],
                n=self.config['n_choices'],
                max_tokens=self.config['max_tokens'],
                presence_penalty=self.config['presence_penalty'],
                frequency_penalty=self.config['frequency_penalty'],
                stream=stream
            )

        except openai.error.RateLimitError as e:
            raise Exception(f'⚠️ _Превышен лимит скорости OpenAI_ ⚠️\n{str(e)}') from e

        except openai.error.InvalidRequestError as e:
            raise Exception(f'⚠️ _OpenAI Неверный запрос_ ⚠️\n{str(e)}') from e

        except Exception as e:
            raise Exception(f'⚠️ _Произошла ошибка_ ⚠️\n{str(e)}') from e

    async def generate_image(self, prompt: str) -> tuple[str, str]:
        """
        Генерирует изображение по заданному запросу, используя модель DALL-E.
        :param prompt: Подсказка, которую нужно отправить в модель
        :return: URL изображения и его размер
        """
        try:
            response = await openai.Image.acreate(
                prompt=prompt,
                n=1,
                size=self.config['image_size']
            )

            if 'data' not in response or len(response['data']) == 0:
                logging.error(f'Нет ответа от GPT: {str(response)}')
                raise Exception('⚠️ _Произошла ошибка_ ⚠️\nПожалуйста, повторите попытку через некоторое время.')

            return response['data'][0]['url'], self.config['image_size']
        except Exception as e:
            raise Exception(f'⚠️ _Произошла ошибка_ ⚠️\n{str(e)}') from e

    async def transcribe(self, filename):
        """
        Транскрибирует аудиофайл, используя модель Whisper.
        """
        try:
            with open(filename, "rb") as audio:
                result = await openai.Audio.atranscribe("whisper-1", audio)
                return result.text
        except Exception as e:
            logging.exception(e)
            raise Exception(f'⚠️ _Произошла ошибка_ ⚠️\n{str(e)}') from e

    def reset_chat_history(self, chat_id, content=''):
        """
        Сброс истории разговоров.
        """
        if content == '':
            content = self.config['assistant_prompt']
        self.conversations[chat_id] = [{"role": "system", "content": content}]

    def __max_age_reached(self, chat_id) -> bool:
        """
        Проверяет, достигнут ли максимальный возраст разговора.
        :param chat_id: ID чата
        :return: Булево значение, указывающее, был ли достигнут максимальный возраст беседы.
        """
        if chat_id not in self.last_updated:
            return False
        last_updated = self.last_updated[chat_id]
        now = datetime.datetime.now()
        max_age_minutes = self.config['max_conversation_age_minutes']
        return last_updated < now - datetime.timedelta(minutes=max_age_minutes)

    def __add_to_history(self, chat_id, role, content):
        """
        Добавляет сообщение в историю разговора.
        :param chat_id: ID чата
        :param role: Роль отправителя сообщения
        :param content: Содержание сообщения
        """
        self.conversations[chat_id].append({"role": role, "content": content})

    async def __summarise(self, conversation) -> str:
        """
        Обобщает историю разговора.
        :param conversation: История разговора
        :return: Краткое содержание
        """
        messages = [
            { "role": "assistant", "content": "Кратко опишите этот разговор в 700 символах или меньше" },
            { "role": "user", "content": str(conversation) }
        ]
        response = await openai.ChatCompletion.acreate(
            model=self.config['model'],
            messages=messages,
            temperature=0.4
        )
        return response.choices[0]['message']['content']

    def __max_model_tokens(self):
        if self.config['model'] in GPT_3_MODELS:
            return 4096
        if self.config['model'] in GPT_4_MODELS:
            return 8192
        if self.config['model'] in GPT_4_32K_MODELS:
            return 32768
        raise NotImplementedError(
            f"Максимальное количество токенов для модели {self.config['model']} пока не реализована."
        )

    # https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb
    def __count_tokens(self, messages) -> int:
        """
        Подсчитывает количество токенов, необходимых для отправки заданных сообщений.
        :param messages: сообщения для отправки
        :return: количество необходимых жетонов
        """
        try:
            model = self.config['model']
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("gpt-3.5-turbo")

        if model in GPT_3_MODELS:
            tokens_per_message = 4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
            tokens_per_name = -1  # if there's a name, the role is omitted
        elif model in GPT_4_MODELS + GPT_4_32K_MODELS:
            tokens_per_message = 3
            tokens_per_name = 1
        else:
            raise NotImplementedError(f"""num_tokens_from_messages() не реализована для модели {model}.""")
        num_tokens = 0
        for message in messages:
            num_tokens += tokens_per_message
            for key, value in message.items():
                num_tokens += len(encoding.encode(value))
                if key == "name":
                    num_tokens += tokens_per_name
        num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
        return num_tokens
    
    def get_billing_current_month(self):
        """Получает из API OpenAI данные об использовании в текущем месяце.

        :return: долларовая сумма использования в текущем месяце
        """
        headers = {
            "Authorization": f"Bearer {openai.api_key}"
        }
        # calculate first and last day of current month
        today = date.today()
        first_day = date(today.year, today.month, 1)
        _, last_day_of_month = monthrange(today.year, today.month)
        last_day = date(today.year, today.month, last_day_of_month)
        params = {
            "start_date": first_day,
            "end_date": last_day
        }
        response = requests.get("https://api.openai.com/dashboard/billing/usage", headers=headers, params=params)
        billing_data = json.loads(response.text)
        usage_month = billing_data["total_usage"] / 100 # convert cent amount to dollars
        return usage_month
