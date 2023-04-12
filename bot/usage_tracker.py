import os.path
import pathlib
import json
from datetime import date

def year_month(date):
    # extract string of year-month from date, eg: '2023-03'
    return str(date)[:7]

class UsageTracker:
    """
    Класс UsageTracker
    Позволяет отслеживать ежедневное/ежемесячное использование для каждого пользователя.
    Файлы пользователей хранятся в виде JSON в каталоге /usage_logs.
    Пример JSON:
    {
        "user_name": "@user_name",
        "current_cost": {
            "day": 0.45,
            "month": 3.23,
            "all_time": 3.23,
            "last_update": "2023-03-14"},
        "usage_history": {
            "chat_tokens": {
                "2023-03-13": 520,
                "2023-03-14": 1532
            },
            "transcription_seconds": {
                "2023-03-13": 125,
                "2023-03-14": 64
            },
            "number_images": {
                "2023-03-12": [0, 2, 3],
                "2023-03-13": [1, 2, 3],
                "2023-03-14": [0, 1, 2]
            }
        }
    }
    """

    def __init__(self, user_id, user_name, logs_dir="usage_logs"):
        """
        Инициализирует UsageTracker для пользователя с текущей датой.
        Загружает данные об использовании из файла журнала использования.
        :param user_id: Telegram ID пользователя
        :param user_name: имя пользователя Telegram
        :param logs_dir: путь к директории журналов использования, по умолчанию "usage_logs".
        """
        self.user_id = user_id
        self.logs_dir = logs_dir
        # path to usage file of given user
        self.user_file = f"{logs_dir}/{user_id}.json"

        if os.path.isfile(self.user_file):
            with open(self.user_file, "r") as file:
                self.usage = json.load(file)
        else:
            # ensure directory exists
            pathlib.Path(logs_dir).mkdir(exist_ok=True)
            # create new dictionary for this user
            self.usage = {
                "user_name": user_name,
                "current_cost": {"day": 0.0, "month": 0.0, "all_time": 0.0, "last_update": str(date.today())},
                "usage_history": {"chat_tokens": {}, "transcription_seconds": {}, "number_images": {}}
            }

    # token usage functions:

    def add_chat_tokens(self, tokens, tokens_price=0.002):
        """Добавляет использованные токены из запроса в историю использования пользователя и обновляет текущую стоимость.
        :param tokens: общее количество токенов, использованных в последнем запросе
        :param tokens_price: цена за 1000 токенов, по умолчанию 0.002
        """
        today = date.today()
        last_update = date.fromisoformat(self.usage["current_cost"]["last_update"])
        token_cost = round(tokens * tokens_price / 1000, 6)
        # add to all_time cost, initialize with calculation of total_cost if key doesn't exist
        self.usage["current_cost"]["all_time"] = self.usage["current_cost"].get("all_time", self.initialize_all_time_cost()) + token_cost
        # add current cost, update new day
        if today == last_update:
            self.usage["current_cost"]["day"] += token_cost
            self.usage["current_cost"]["month"] += token_cost
        else:
            if today.month == last_update.month:
                self.usage["current_cost"]["month"] += token_cost
            else:
                self.usage["current_cost"]["month"] = token_cost
            self.usage["current_cost"]["day"] = token_cost
            self.usage["current_cost"]["last_update"] = str(today)
        # update usage_history
        if str(today) in self.usage["usage_history"]["chat_tokens"]:
            # add token usage to existing date
            self.usage["usage_history"]["chat_tokens"][str(today)] += tokens
        else:
            # create new entry for current date
            self.usage["usage_history"]["chat_tokens"][str(today)] = tokens
        
        # write updated token usage to user file
        with open(self.user_file, "w") as outfile:
            json.dump(self.usage, outfile)

    def get_current_token_usage(self):
        """Получить количество токенов, использованных за сегодня и за месяц

        :return: общее количество токенов, использованных за день и за месяц
        """
        today = date.today()
        if str(today) in self.usage["usage_history"]["chat_tokens"]:
            usage_day = self.usage["usage_history"]["chat_tokens"][str(today)]
        else:
            usage_day = 0
        month = str(today)[:7] # year-month as string
        usage_month = 0
        for today, tokens in self.usage["usage_history"]["chat_tokens"].items():
            if today.startswith(month):
                usage_month += tokens
        return usage_day, usage_month

    # image usage functions:

    def add_image_request(self, image_size, image_prices="0.016,0.018,0.02"):
        """Добавляет запрос изображения в историю использования пользователями и обновляет текущие расходы.

        :param image_size: размер запрашиваемого изображения
        :param image_prices: цены для изображений размеров ["256x256", "512x512", "1024x1024"],
                             по умолчанию [0.016, 0.018, 0.02]
        """
        sizes = ["256x256", "512x512", "1024x1024"]
        requested_size = sizes.index(image_size)
        image_cost = image_prices[requested_size]

        today = date.today()
        last_update = date.fromisoformat(self.usage["current_cost"]["last_update"])
        # add to all_time cost, initialize with calculation of total_cost if key doesn't exist
        self.usage["current_cost"]["all_time"] = self.usage["current_cost"].get("all_time", self.initialize_all_time_cost()) + image_cost
        # add current cost, update new day
        if today == last_update:
            self.usage["current_cost"]["day"] += image_cost
            self.usage["current_cost"]["month"] += image_cost
        else:
            if today.month == last_update.month:
                self.usage["current_cost"]["month"] += image_cost
            else:
                self.usage["current_cost"]["month"] = image_cost
            self.usage["current_cost"]["day"] = image_cost
            self.usage["current_cost"]["last_update"] = str(today)

        # update usage_history
        if str(today) in self.usage["usage_history"]["number_images"]:
            # add token usage to existing date
            self.usage["usage_history"]["number_images"][str(today)][requested_size] += 1
        else:
            # create new entry for current date
            self.usage["usage_history"]["number_images"][str(today)] = [0, 0, 0]
            self.usage["usage_history"]["number_images"][str(today)][requested_size] += 1
        
        # write updated image number to user file
        with open(self.user_file, "w") as outfile:
            json.dump(self.usage, outfile)

    def get_current_image_count(self):
        """Получить количество изображений, запрошенных за сегодня и за месяц.

        :return: общее количество изображений, запрошенных за день и за месяц
        """
        today=date.today()
        if str(today) in self.usage["usage_history"]["number_images"]:
            usage_day = sum(self.usage["usage_history"]["number_images"][str(today)])
        else:
            usage_day = 0
        month = str(today)[:7] # year-month as string
        usage_month = 0
        for today, images in self.usage["usage_history"]["number_images"].items():
            if today.startswith(month):
                usage_month += sum(images)
        return usage_day, usage_month

    # transcription usage functions:

    def add_transcription_seconds(self, seconds, minute_price=0.006):
        """Добавляет запрошенные транскрипционные секунды в историю использования пользователя и обновляет текущую стоимость.
        :param tokens: общее количество токенов, использованных в последнем запросе
        :param tokens_price: цена за минуту транскрипции, по умолчанию 0.006
        """
        today = date.today()
        last_update = date.fromisoformat(self.usage["current_cost"]["last_update"])
        transcription_price = round(seconds * minute_price / 60, 2)
        # add to all_time cost, initialize with calculation of total_cost if key doesn't exist
        self.usage["current_cost"]["all_time"] = self.usage["current_cost"].get("all_time", self.initialize_all_time_cost()) + transcription_price
        # add current cost, update new day
        if today == last_update:
            self.usage["current_cost"]["day"] += transcription_price
            self.usage["current_cost"]["month"] += transcription_price
        else:
            if today.month == last_update.month:
                self.usage["current_cost"]["month"] += transcription_price
            else:
                self.usage["current_cost"]["month"] = transcription_price
            self.usage["current_cost"]["day"] = transcription_price
            self.usage["current_cost"]["last_update"] = str(today)

        # update usage_history
        if str(today) in self.usage["usage_history"]["transcription_seconds"]:
            # add requested seconds to existing date
            self.usage["usage_history"]["transcription_seconds"][str(today)] += seconds
        else:
            # create new entry for current date
            self.usage["usage_history"]["transcription_seconds"][str(today)] = seconds
        
        # write updated token usage to user file
        with open(self.user_file, "w") as outfile:
            json.dump(self.usage, outfile)

    def get_current_transcription_duration(self):
        """Получает минуты и секунды аудиозаписи за сегодня и за месяц.

        :return: общее количество времени, транскрибированного за день и за месяц (4 значения)
        """
        today = date.today()
        if str(today) in self.usage["usage_history"]["transcription_seconds"]:
            seconds_day = self.usage["usage_history"]["transcription_seconds"][str(today)]
        else:
            seconds_day = 0
        month = str(today)[:7] # year-month as string
        seconds_month = 0
        for today, seconds in self.usage["usage_history"]["transcription_seconds"].items():
            if today.startswith(month):
                seconds_month += seconds
        minutes_day, seconds_day = divmod(seconds_day, 60)
        minutes_month, seconds_month = divmod(seconds_month, 60)
        return int(minutes_day), round(seconds_day, 2), int(minutes_month), round(seconds_month, 2)
    
    # general functions
    def get_current_cost(self):
        """Получить общую сумму в USD по всем запросам текущего дня и месяца

        :return: стоимость текущего дня и месяца
        """
        today = date.today()
        last_update = date.fromisoformat(self.usage["current_cost"]["last_update"])
        if today == last_update:
            cost_day = self.usage["current_cost"]["day"]
            cost_month = self.usage["current_cost"]["month"]
        else:
            cost_day = 0.0
            if today.month == last_update.month:
                cost_month = self.usage["current_cost"]["month"]
            else:
                cost_month = 0.0
        # add to all_time cost, initialize with calculation of total_cost if key doesn't exist
        cost_all_time = self.usage["current_cost"].get("all_time", self.initialize_all_time_cost())
        return {"cost_today": cost_day, "cost_month": cost_month, "cost_all_time": cost_all_time}

    def initialize_all_time_cost(self, tokens_price=0.002, image_prices="0.016,0.018,0.02", minute_price=0.006):
        """Получение общей суммы в USD по всем запросам в истории
        
        :param tokens_price: цена за 1000 токенов, по умолчанию 0.002
        :param image_prices: цены для изображений размеров ["256x256", "512x512", "1024x1024"],
            по умолчанию [0.016, 0.018, 0.02]
        :param tokens_price: цена за минуту транскрипции, по умолчанию 0.006
        :return: общая стоимость всех запросов
        """
        total_tokens = sum(self.usage['usage_history']['chat_tokens'].values())
        token_cost = round(total_tokens * tokens_price / 1000, 6)
        
        total_images = [sum(values) for values in zip(*self.usage['usage_history']['number_images'].values())]
        image_prices_list = [float(x) for x in image_prices.split(',')]
        image_cost = sum([count * price for count, price in zip(total_images, image_prices_list)])
        
        total_transcription_seconds = sum(self.usage['usage_history']['transcription_seconds'].values())
        transcription_cost = round(total_transcription_seconds * minute_price / 60, 2)

        all_time_cost = token_cost + transcription_cost + image_cost
        return all_time_cost
