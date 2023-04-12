from __future__ import annotations
import logging
import os
import itertools
import asyncio

import telegram
from uuid import uuid4
from telegram import constants, BotCommandScopeAllGroupChats
from telegram import Message, MessageEntity, Update, InlineQueryResultArticle, InputTextMessageContent, BotCommand, ChatMember
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, \
    filters, InlineQueryHandler, Application, CallbackContext

from pydub import AudioSegment
from openai_helper import OpenAIHelper
from usage_tracker import UsageTracker


def message_text(message: Message) -> str:
    """
    Возвращает текст сообщения, исключая любые команды бота.
    """
    message_text = message.text
    if message_text is None:
        return ''

    for _, text in sorted(message.parse_entities([MessageEntity.BOT_COMMAND]).items(), key=(lambda item: item[0].offset)):
        message_text = message_text.replace(text, '').strip()

    return message_text if len(message_text) > 0 else ''


class ChatGPTTelegramBot:
    """
    Класс, представляющий Telegram-бота ChatGPT.
    """
    # Mapping of budget period to cost period
    budget_cost_map = {
            "monthly":"cost_month",
            "daily":"cost_today",
            "all-time":"cost_all_time"
        }
    # Mapping of budget period to a print output
    budget_print_map = {
        "monthly": " this month",
        "daily": " today",
        "all-time": ""
    }

    def __init__(self, config: dict, openai: OpenAIHelper):
        """
        Инициализирует бота с заданной конфигурацией и объектом бота GPT.
        :param config: Словарь, содержащий конфигурацию бота
        :param openai: Объект OpenAIHelper
        """
        self.config = config
        self.openai = openai
        self.commands = [
            BotCommand(command='help', description='Показать сообщение справки'),
            BotCommand(command='reset', description='Перезагрузка разговора. Опционально передавать желаемую модель поведения '
                                                    '(например, /reset Вы - полезный помощник)'),
            BotCommand(command='image', description='Создайте изображение из запроса (например, /image cat)'),
            BotCommand(command='stats', description='Получите статистику текущего использования'),
            BotCommand(command='resend', description='Повторная отправка последнего сообщения')
        ]
        self.group_commands = [
            BotCommand(command='chat', description='Общайтесь с ботом!')
        ] + self.commands
        self.disallowed_message = "Извините, вам не разрешено использовать этого бота.  " \
                                  " "
        self.budget_limit_message = f"Извините, вы достигли лимита использования{self.budget_print_map[config['budget_period']]}."
        self.usage = {}
        self.last_message = {}

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Shows the help menu.
        """
        commands = self.group_commands if self.is_group_chat(update) else self.commands
        commands_description = [f'/{command.command} - {command.description}' for command in commands]
        help_text = 'Привет, дружище! Могу сказать, что ты выглядишь отлично! Меня зовут Виктор и я готов оказать вам любую необходимую помощь. Так что, , напиши чем тебе помочь?'                   
        await update.message.reply_text(help_text, disable_web_page_preview=True)
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Возвращает статистику использования токенов за текущий день и месяц.
        """
        if not await self.is_allowed(update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                f'не имеет права запрашивать статистику их использования')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
            f'запросили статистику их использования')
        
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        tokens_today, tokens_month = self.usage[user_id].get_current_token_usage()
        images_today, images_month = self.usage[user_id].get_current_image_count()
        (transcribe_minutes_today, transcribe_seconds_today, transcribe_minutes_month, 
            transcribe_seconds_month) = self.usage[user_id].get_current_transcription_duration()
        current_cost = self.usage[user_id].get_current_cost()
        
        chat_id = update.effective_chat.id
        chat_messages, chat_token_length = self.openai.get_conversation_stats(chat_id)
        remaining_budget = self.get_remaining_budget(update)

        text_current_conversation = f"*Текущий разговор:*\n"+\
                     f"{chat_messages} сообщения чата в истории.\n"+\
                     f"{chat_token_length} токены чата в истории.\n"+\
                     f"----------------------------\n"
        text_today = f"*Использование сегодня:*\n"+\
                     f"{tokens_today} используемые токены чата.\n"+\
                     f"{images_today} создаваемые изображения.\n"+\
                     f"{transcribe_minutes_today} минуты и {transcribe_seconds_today} секунды расшифрованы.\n"+\
                     f"💰 На общую сумму ${current_cost['cost_today']:.2f}\n"+\
                     f"----------------------------\n"
        text_month = f"*Использование в этом месяце:*\n"+\
                     f"{tokens_month} используемые токены чата.\n"+\
                     f"{images_month} создаваемые изображения.\n"+\
                     f"{transcribe_minutes_month} минуты и {transcribe_seconds_month} секунды расшифрованы.\n"+\
                     f"💰 На общую сумму ${current_cost['cost_month']:.2f}"
        # text_budget filled with conditional content
        text_budget = "\n\n"
        budget_period =self.config['budget_period']
        if remaining_budget < float('inf'):
            text_budget += f"У вас есть оставшийся бюджет в размере ${remaining_budget:.2f}{self.budget_print_map[budget_period]}.\n"
        # add OpenAI account information for admin request
        if self.is_admin(update):
            text_budget += f"На ваш счет в OpenAI был выставлен счет ${self.openai.get_billing_current_month():.2f} this month."
        
        usage_text = text_current_conversation + text_today + text_month + text_budget
        await update.message.reply_text(usage_text, parse_mode=constants.ParseMode.MARKDOWN)

    async def resend(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Повторная отправка последнего запроса
        """
        if not await self.is_allowed(update, context):
            logging.warning(f'User {update.message.from_user.name}  (id: {update.message.from_user.id})'
                            f' не имеет права повторно отправлять сообщение')
            await self.send_disallowed_message(update, context)
            return

        chat_id = update.effective_chat.id
        if chat_id not in self.last_message:
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id})'
                            f' не имеет ничего для повторной отправки')
            await context.bot.send_message(chat_id=chat_id, text="You have nothing to resend")
            return

        # Update message text, clear self.last_message and send the request to prompt
        logging.info(f'Повторная отправка последнего запроса от пользователя: {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})')
        with update.message._unfrozen() as message:
            message.text = self.last_message.pop(chat_id)

        await self.prompt(update=update, context=context)

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Сброс разговора.
        """
        if not await self.is_allowed(update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                f'не разрешается сбрасывать разговор')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'Сброс разговора для пользователя {update.message.from_user.name} '
            f'(id: {update.message.from_user.id})...')

        chat_id = update.effective_chat.id
        reset_content = message_text(update.message)
        self.openai.reset_chat_history(chat_id=chat_id, content=reset_content)
        await context.bot.send_message(chat_id=chat_id, text='Done!')

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Генерирует изображение для заданного запроса, используя API DALL-E
        """
        if not self.config['enable_image_generation'] or not await self.check_allowed_and_within_budget(update, context):
            return

        chat_id = update.effective_chat.id
        image_query = message_text(update.message)
        if image_query == '':
            await context.bot.send_message(chat_id=chat_id, text='Пожалуйста, предоставьте запрос! (например, /image cat)')
            return

        logging.info(f'Запрос на создание нового изображения, полученный от пользователя {update.message.from_user.name} '
            f'(id: {update.message.from_user.id})')

        async def _generate():
            try:
                image_url, image_size = await self.openai.generate_image(prompt=image_query)
                await context.bot.send_photo(
                    chat_id=chat_id,
                    reply_to_message_id=self.get_reply_to_message_id(update),
                    photo=image_url
                )
                # add image request to users usage tracker
                user_id = update.message.from_user.id
                self.usage[user_id].add_image_request(image_size, self.config['image_prices'])
                # add guest chat request to guest usage tracker
                if str(user_id) not in self.config['allowed_user_ids'].split(',') and 'guests' in self.usage:
                    self.usage["guests"].add_image_request(image_size, self.config['image_prices'])

            except Exception as e:
                logging.exception(e)
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=self.get_reply_to_message_id(update),
                    text=f'Не удалось сгенерировать изображение: {str(e)}',
                    parse_mode=constants.ParseMode.MARKDOWN
                )

        await self.wrap_with_indicator(update, context, constants.ChatAction.UPLOAD_PHOTO, _generate)

    async def transcribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Расшифровка аудиосообщений.
        """
        if not self.config['enable_transcription'] or not await self.check_allowed_and_within_budget(update, context):
            return

        if self.is_group_chat(update) and self.config['ignore_group_transcriptions']:
            logging.info(f'Транскрипция идет из группового чата, игнорирование...')
            return

        chat_id = update.effective_chat.id
        filename = update.message.effective_attachment.file_unique_id

        async def _execute():
            filename_mp3 = f'{filename}.mp3'

            try:
                media_file = await context.bot.get_file(update.message.effective_attachment.file_id)
                await media_file.download_to_drive(filename)
            except Exception as e:
                logging.exception(e)
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=self.get_reply_to_message_id(update),
                    text=f'Не удалось загрузить аудиофайл: {str(e)}. Убедитесь, что файл не слишком большой. (max 20MB)',
                    parse_mode=constants.ParseMode.MARKDOWN
                )
                return

            # detect and extract audio from the attachment with pydub
            try:
                audio_track = AudioSegment.from_file(filename)
                audio_track.export(filename_mp3, format="mp3")
                logging.info(f'Получен новый запрос на расшифровку от пользователя {update.message.from_user.name} '
                    f'(id: {update.message.from_user.id})')

            except Exception as e:
                logging.exception(e)
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    reply_to_message_id=self.get_reply_to_message_id(update),
                    text='Неподдерживаемый тип файла'
                )
                if os.path.exists(filename):
                    os.remove(filename)
                return

            user_id = update.message.from_user.id
            if user_id not in self.usage:
                self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

            # send decoded audio to openai
            try:

                # Transcribe the audio file
                transcript = await self.openai.transcribe(filename_mp3)

                # add transcription seconds to usage tracker
                transcription_price = self.config['transcription_price']
                self.usage[user_id].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

                # add guest chat request to guest usage tracker
                allowed_user_ids = self.config['allowed_user_ids'].split(',')
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

                if self.config['voice_reply_transcript']:

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    transcript_output = f'_Transcript:_\n"{transcript}"'
                    chunks = self.split_into_chunks(transcript_output)

                    for index, transcript_chunk in enumerate(chunks):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            reply_to_message_id=self.get_reply_to_message_id(update) if index == 0 else None,
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )
                else:
                    # Get the response of the transcript
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=transcript)

                    # add chat request to users usage tracker
                    self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                    # add guest chat request to guest usage tracker
                    if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                        self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    transcript_output = f'_Расшифровка:_\n"{transcript}"\n\n_Answer:_\n{response}'
                    chunks = self.split_into_chunks(transcript_output)

                    for index, transcript_chunk in enumerate(chunks):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            reply_to_message_id=self.get_reply_to_message_id(update) if index == 0 else None,
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )

            except Exception as e:
                logging.exception(e)
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=self.get_reply_to_message_id(update),
                    text=f'Не удалось расшифровать текст: {str(e)}',
                    parse_mode=constants.ParseMode.MARKDOWN
                )
            finally:
                # Cleanup files
                if os.path.exists(filename_mp3):
                    os.remove(filename_mp3)
                if os.path.exists(filename):
                    os.remove(filename)

        await self.wrap_with_indicator(update, context, constants.ChatAction.TYPING, _execute)

    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Реагировать на входящие сообщения и отвечать соответствующим образом.
        """
        if not await self.check_allowed_and_within_budget(update, context):
            return
        
        logging.info(f'Новое сообщение, полученное от пользователя {update.message.from_user.name} (id: {update.message.from_user.id})')
        chat_id = update.effective_chat.id
        user_id = update.message.from_user.id
        prompt = message_text(update.message)
        self.last_message[chat_id] = prompt

        if self.is_group_chat(update):
            trigger_keyword = self.config['group_trigger_keyword']
            if prompt.lower().startswith(trigger_keyword.lower()):
                prompt = prompt[len(trigger_keyword):].strip()
            else:
                if update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id:
                    logging.info('Сообщение - это ответ боту, позволяющий...')
                else:
                    logging.warning('Сообщение не начинается с ключевого слова триггера, игнорирование...')
                    return

        try:
            if self.config['stream']:
                await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
                is_group_chat = self.is_group_chat(update)

                stream_response = self.openai.get_chat_response_stream(chat_id=chat_id, query=prompt)
                i = 0
                prev = ''
                sent_message = None
                backoff = 0
                chunk = 0

                async for content, tokens in stream_response:
                    if len(content.strip()) == 0:
                        continue

                    chunks = self.split_into_chunks(content)
                    if len(chunks) > 1:
                        content = chunks[-1]
                        if chunk != len(chunks) - 1:
                            chunk += 1
                            try:
                                await self.edit_message_with_retry(context, chat_id, sent_message.message_id, chunks[-2])
                            except:
                                pass
                            try:
                                sent_message = await context.bot.send_message(
                                    chat_id=sent_message.chat_id,
                                    text=content if len(content) > 0 else "..."
                                )
                            except:
                                pass
                            continue

                    if is_group_chat:
                        # group chats have stricter flood limits
                        cutoff = 180 if len(content) > 1000 else 120 if len(content) > 200 else 90 if len(content) > 50 else 50
                    else:
                        cutoff = 90 if len(content) > 1000 else 45 if len(content) > 200 else 25 if len(content) > 50 else 15

                    cutoff += backoff

                    if i == 0:
                        try:
                            if sent_message is not None:
                                await context.bot.delete_message(chat_id=sent_message.chat_id,
                                                                 message_id=sent_message.message_id)
                            sent_message = await context.bot.send_message(
                                chat_id=chat_id,
                                reply_to_message_id=self.get_reply_to_message_id(update),
                                text=content
                            )
                        except:
                            continue

                    elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                        prev = content

                        try:
                            use_markdown = tokens != 'not_finished'
                            await self.edit_message_with_retry(context, chat_id, sent_message.message_id,
                                                               text=content, markdown=use_markdown)

                        except RetryAfter as e:
                            backoff += 5
                            await asyncio.sleep(e.retry_after)
                            continue

                        except TimedOut:
                            backoff += 5
                            await asyncio.sleep(0.5)
                            continue

                        except Exception:
                            backoff += 5
                            continue

                        await asyncio.sleep(0.01)

                    i += 1
                    if tokens != 'not_finished':
                        total_tokens = int(tokens)

            else:
                async def _reply():
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=prompt)

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    chunks = self.split_into_chunks(response)

                    for index, chunk in enumerate(chunks):
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                reply_to_message_id=self.get_reply_to_message_id(update) if index == 0 else None,
                                text=chunk,
                                parse_mode=constants.ParseMode.MARKDOWN
                            )
                        except Exception:
                            try:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    reply_to_message_id=self.get_reply_to_message_id(update) if index == 0 else None,
                                    text=chunk
                                )
                            except Exception as e:
                                raise e

                await self.wrap_with_indicator(update, context, constants.ChatAction.TYPING, _reply)

            try:
                # add chat request to users usage tracker
                self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                # add guest chat request to guest usage tracker
                allowed_user_ids = self.config['allowed_user_ids'].split(',')
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])
            except:
                pass

        except Exception as e:
            logging.exception(e)
            await context.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=self.get_reply_to_message_id(update),
                text=f'Failed to get response: {str(e)}',
                parse_mode=constants.ParseMode.MARKDOWN
            )

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Обработка встроенного запроса. Он выполняется, когда вы вводите: @botusername <query>
        """
        query = update.inline_query.query

        if query == '':
            return

        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title='Ask ChatGPT',
                input_message_content=InputTextMessageContent(query),
                description=query,
                thumb_url='https://user-images.githubusercontent.com/11541888/223106202-7576ff11-2c8e-408d-94ea-b02a7a32149a.png'
            )
        ]

        await update.inline_query.answer(results)

    async def edit_message_with_retry(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                      message_id: int, text: str, markdown: bool = True):
        """
        Редактирование сообщения с логикой повторной попытки в случае неудачи (например, неработающий маркдаун)
        :param context: Используемый контекст
        :param chat_id: Идентификатор чата для редактирования сообщения
        :param message_id: Идентификатор сообщения для редактирования
        :param text: Текст для редактирования сообщения
        :param markdown: Использовать ли режим разбора markdown
        :return: None
        """
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=constants.ParseMode.MARKDOWN if markdown else None
            )
        except telegram.error.BadRequest as e:
            if str(e).startswith("Message is not modified"):
                return
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text
                )
            except Exception as e:
                logging.warning(f'Failed to edit message: {str(e)}')
                raise e

        except Exception as e:
            logging.warning(str(e))
            raise e

    async def wrap_with_indicator(self, update: Update, context: CallbackContext, chat_action: constants.ChatAction, coroutine):
        """
        Обертывает coroutine при повторной отправке действия чата пользователю.
        """
        task = context.application.create_task(coroutine(), update=update)
        while not task.done():
            context.application.create_task(update.effective_chat.send_action(chat_action))
            try:
                await asyncio.wait_for(asyncio.shield(task), 4.5)
            except asyncio.TimeoutError:
                pass

    async def send_disallowed_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Отправляет запрещенное сообщение пользователю.
        """
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=self.disallowed_message,
            disable_web_page_preview=True
        )

    async def send_budget_reached_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Отправляет пользователю сообщение о достигнутом бюджете.
        """
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=self.budget_limit_message
        )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Обработка ошибок в библиотеке telegram-python-bot.
        """
        logging.error(f'Исключение при обработке обновления: {context.error}')

    def is_group_chat(self, update: Update) -> bool:
        """
        Проверяет, было ли сообщение отправлено из группового чата.
        """
        return update.effective_chat.type in [
            constants.ChatType.GROUP,
            constants.ChatType.SUPERGROUP
        ]

    async def is_user_in_group(self, update: Update, context: CallbackContext, user_id: int) -> bool:
        """
        Проверяет, является ли user_id членом группы
        """
        try:
            chat_member = await context.bot.get_chat_member(update.message.chat_id, user_id)
            return chat_member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER]
        except telegram.error.BadRequest as e:
            if str(e) == "Пользователь не найден":
                return False
            else:
                raise e
        except Exception as e:
            raise e

    async def is_allowed(self, update: Update, context: CallbackContext) -> bool:
        """
        Проверяет, разрешено ли пользователю использовать бота.
        """
        if self.config['allowed_user_ids'] == '*':
            return True
        
        if self.is_admin(update):
            return True
        
        allowed_user_ids = self.config['allowed_user_ids'].split(',')
        # Check if user is allowed
        if str(update.message.from_user.id) in allowed_user_ids:
            return True

        # Check if it's a group a chat with at least one authorized member
        if self.is_group_chat(update):
            admin_user_ids = self.config['admin_user_ids'].split(',')
            for user in itertools.chain(allowed_user_ids, admin_user_ids):
                if not user.strip():
                    continue
                if await self.is_user_in_group(update, context, user):
                    logging.info(f'{user} является членом группы. Разрешение сообщений группового чата...')
                    return True
            logging.info(f'Сообщения группового чата от пользователя {update.message.from_user.name} '
                f'(id: {update.message.from_user.id}) не допускаются')

        return False

    def is_admin(self, update: Update, log_no_admin=False) -> bool:
        """
        Проверяет, является ли пользователь администратором бота.
        Первый пользователь в списке пользователей является администратором.
        """
        if self.config['admin_user_ids'] == '-':
            if log_no_admin:
                logging.info('Не определен пользователь-администратор.')
            return False

        admin_user_ids = self.config['admin_user_ids'].split(',')

        # Check if user is in the admin user list
        if str(update.message.from_user.id) in admin_user_ids:
            return True

        return False

    def get_user_budget(self, update: Update) -> float | None:
        """
        Получение бюджета пользователя на основе его идентификатора пользователя и конфигурации бота.
        :param update: Объект обновления Telegram
        :return: Бюджет пользователя в виде float, или None, если пользователь не найден в списке разрешенных пользователей.
        """
        
        # no budget restrictions for admins and '*'-budget lists
        if self.is_admin(update) or self.config['user_budgets'] == '*':
            return float('inf')
        
        user_budgets = self.config['user_budgets'].split(',')
        if self.config['allowed_user_ids'] == '*':
            # same budget for all users, use value in first position of budget list
            if len(user_budgets) > 1:
                logging.warning('несколько значений для бюджетов, установленных с неограниченным списком пользователей '
                                'только первое значение используется в качестве бюджета для всех.')
            return float(user_budgets[0])

        user_id = update.message.from_user.id
        allowed_user_ids = self.config['allowed_user_ids'].split(',')
        if str(user_id) in allowed_user_ids:
            user_index = allowed_user_ids.index(str(user_id))
            if len(user_budgets) <= user_index:
                logging.warning(f'Для идентификатора пользователя не установлен бюджет: {user_id}. Бюджетный список короче, чем список пользователей.')
                return 0.0
            return float(user_budgets[user_index])
        return None

    def get_remaining_budget(self, update: Update) -> float:
        """
        Рассчитывает оставшийся бюджет для пользователя на основе его текущего использования.
        :param update: объект обновления Telegram
        :return: Оставшийся бюджет пользователя в виде плавающего значения.
        """
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)
        
        # Get budget for users
        user_budget = self.get_user_budget(update)
        budget_period = self.config['budget_period']
        if user_budget is not None:
            cost = self.usage[user_id].get_current_cost()[self.budget_cost_map[budget_period]]
            return user_budget - cost

        # Get budget for guests
        if 'guests' not in self.usage:
            self.usage['guests'] = UsageTracker('guests', 'all guest users in group chats')
        cost = self.usage['guests'].get_current_cost()[self.budget_cost_map[budget_period]]
        return self.config['guest_budget'] - cost

    def is_within_budget(self, update: Update) -> bool:
        """
        Проверяет, достиг ли пользователь предела использования.
        При необходимости инициализирует UsageTracker для пользователя и гостя.
        :param update: объект обновления Telegram
        :return: Булево значение, указывающее, есть ли у пользователя положительный бюджет.
        """
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        remaining_budget = self.get_remaining_budget(update)

        return remaining_budget > 0

    async def check_allowed_and_within_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """
        Проверяет, разрешено ли пользователю использовать бота и находится ли он в пределах своего бюджета.
        :param update: объект обновления Telegram
        :param context: Объект контекста Telegram
        :return: Булево значение, указывающее, разрешено ли пользователю использовать бота.
        """
        if not await self.is_allowed(update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                f'не имеет права использовать бота')
            await self.send_disallowed_message(update, context)
            return False

        if not self.is_within_budget(update):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                f'достигли лимита использования')
            await self.send_budget_reached_message(update, context)
            return False

        return True

    def get_reply_to_message_id(self, update: Update):
        """
        Возвращает идентификатор сообщения, на которое нужно ответить.
        :param update: объект обновления Telegram
        :return: id сообщения, на которое нужно ответить, или None, если цитирование отключено.
        """
        if self.config['enable_quoting'] or self.is_group_chat(update):
            return update.message.message_id
        return None

    def split_into_chunks(self, text: str, chunk_size: int = 4096) -> list[str]:
        """
        Разделяет строку на фрагменты заданного размера.
        """
        return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

    async def post_init(self, application: Application) -> None:
        """
        Инициализационный крючок для бота.
        """
        await application.bot.set_my_commands(self.group_commands, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(self.commands)

    def run(self):
        """
        Запускает бота на неопределенное время, пока пользователь не нажмет Ctrl+C
        """
        application = ApplicationBuilder() \
            .token(self.config['token']) \
            .proxy_url(self.config['proxy']) \
            .get_updates_proxy_url(self.config['proxy']) \
            .post_init(self.post_init) \
            .concurrent_updates(True) \
            .build()

        application.add_handler(CommandHandler('reset', self.reset))
        application.add_handler(CommandHandler('help', self.help))
        application.add_handler(CommandHandler('image', self.image))
        application.add_handler(CommandHandler('start', self.help))
        application.add_handler(CommandHandler('stats', self.stats))
        application.add_handler(CommandHandler('resend', self.resend))
        application.add_handler(CommandHandler(
            'chat', self.prompt, filters=filters.ChatType.GROUP | filters.ChatType.SUPERGROUP)
        )
        application.add_handler(MessageHandler(
            filters.AUDIO | filters.VOICE | filters.Document.AUDIO |
            filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO,
            self.transcribe))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.prompt))
        application.add_handler(InlineQueryHandler(self.inline_query, chat_types=[
            constants.ChatType.GROUP, constants.ChatType.SUPERGROUP
        ]))

        application.add_error_handler(self.error_handler)

        application.run_polling()
