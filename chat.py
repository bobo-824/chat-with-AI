import os
import sys

from openai import OpenAI
from server import ChatServer, describe_error


def choose_model(client, current_model=None, fallback_models=None):
    try:
        models = sorted(model.id for model in client.models.list())
    except Exception as error:
        print(f"Error fetching models: {describe_error(error, getattr(client, 'api_key', None))}")
        models = []

    if not models:
        configured_models = os.environ.get("OPENAI_MODELS", "")
        models = [model.strip() for model in configured_models.split(",") if model.strip()]
    if not models and fallback_models:
        models = list(fallback_models)

    if not models:
        print("No models available. Please set OPENAI_MODELS and try again.")
        return current_model

    print("\nAvailable models:")
    for index, model in enumerate(models, start=1):
        marker = " (current)" if model == current_model else ""
        print(f"{index}. {model}{marker}")

    while True:
        try:
            choice = input("Select model: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return current_model

        if choice == "":
            return current_model

        if not choice.isdigit() or not 1 <= int(choice) <= len(models):
            print(f"Please enter a number from 1 to {len(models)}")
            continue

        selected_model = models[int(choice) - 1]
        print(f"Model changed to: {selected_model}")
        return selected_model


def load_configuration(config_path=None):
    """Load environment overrides and the same local config used by the web app."""
    settings = ChatServer(app_password="", config_path=config_path)
    return settings.api_key, settings.base_url, settings.default_model, settings.configured_models


def main():
    api_key, base_url, model, configured_models = load_configuration()

    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", api_key),
            ("OPENAI_BASE_URL", base_url),
        )
        if not value
    ]
    if missing:
        print("缺少配置：" + ", ".join(missing) + "。")
        print("请先运行 python server.py，在网页左侧栏“连接设置”里保存一次 API URL 和 Key；或设置对应的环境变量后重试。")
        return 1

    client = OpenAI(api_key=api_key, base_url=base_url)
    if not model:
        model = choose_model(client, fallback_models=configured_models)
        if not model:
            return 1

    messages = []

    print("AI chat started. Type /model to change models, or exit/quit to leave.")
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            return 0

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Bye!")
            return 0
        if user_input.lower() == "/model":
            selected_model = choose_model(client, model)
            if selected_model:
                model = selected_model
            continue

        messages.append({"role": "user", "content": user_input})

        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
            )
        except Exception as error:
            messages.pop()
            print(f"Error: {describe_error(error, api_key)}")
            continue

        print("AI: ", end="", flush=True)
        reply_parts = []
        try:
            for chunk in response:
                if not chunk.choices:
                    continue

                content = chunk.choices[0].delta.content
                if content:
                    reply_parts.append(content)
                    print(content, end="", flush=True)
        except Exception as error:
            reply = "".join(reply_parts)
            if reply:
                messages.append({"role": "assistant", "content": reply})
                print()
            print(f"Error: {describe_error(error, api_key)}")
            continue

        reply = "".join(reply_parts)
        messages.append({"role": "assistant", "content": reply})
        print()


if __name__ == "__main__":
    sys.exit(main())
