import base64
import binascii
import json
from dataclasses import dataclass, field

import httpx

from app.config import Settings
from app.prompts import Media, Prompt, UserError

IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "create_or_edit_image",
        "description": (
            "Generate an image, or edit the images attached to the user's request. "
            "Use for background removal, visual transformations, and image creation. "
            "The resulting files are automatically delivered to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed image creation/edit instructions",
                },
                "use_input_images": {
                    "type": "boolean",
                    "description": "True to edit attached images",
                },
            },
            "required": ["prompt", "use_input_images"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class Answer:
    text: str = ""
    media: list[Media] = field(default_factory=list)
    tool_calls: int = 0


def decode_image(url: str) -> Media:
    if not url.startswith("data:image/") or ";base64," not in url:
        raise UserError(
            "The image provider must return base64 image data. URL-only responses are unsupported."
        )
    header, encoded = url.split(",", 1)
    mime = header[5:].split(";")[0]
    extensions = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
    if mime not in extensions or len(encoded) > 28_000_000:
        raise UserError(
            "The provider returned an unsupported image format or an image larger than 20 MB."
        )
    try:
        data = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raise UserError("The provider returned invalid image data.") from None
    if not data:
        raise UserError("The image provider returned an empty image.")
    return Media(f"moose-result.{extensions[mime]}", mime, data)


class Agent:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def post(self, path: str, payload: dict) -> dict:
        if not self.settings.api_key:
            raise UserError("Add an AI API key in Connection settings first.")
        try:
            response = await self.client.post(
                self.settings.base_url + path,
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
                timeout=self.settings.request_timeout,
            )
            response.raise_for_status()
            result = response.json()
            if "error" in result:
                raise UserError(
                    "The AI provider reported an error. Check the model and its supported capabilities."
                )
            return result
        except httpx.TimeoutException:
            raise UserError(
                "The AI provider timed out. Try again or increase the timeout."
            ) from None
        except httpx.HTTPStatusError as exc:
            raise UserError(
                f"AI provider returned HTTP {exc.response.status_code}. Check credentials, credits, and model capabilities."
            ) from None
        except (httpx.HTTPError, ValueError):
            raise UserError(
                "Could not read a response from the AI provider. Check the API base URL."
            ) from None

    @staticmethod
    def message(response: dict) -> dict:
        try:
            return response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise UserError("The provider did not return a chat completion.") from None

    @staticmethod
    def images(message: dict) -> list[Media]:
        images = list(message.get("images") or [])
        if isinstance(message.get("content"), list):
            images.extend(p for p in message["content"] if p.get("type") == "image_url")
        if len(images) > 4:
            raise UserError("The provider returned too many images (maximum 4).")
        return [decode_image(item.get("image_url", {}).get("url", "")) for item in images]

    async def image(self, instruction: str, inputs: list[Media]) -> list[Media]:
        if not self.settings.image_model:
            raise UserError("Choose an image model in Agent settings to enable image editing.")
        if self.settings.image_api == "images":
            # OpenAI-compatible generation endpoint; editing uses multimodal chat mode.
            if inputs:
                raise UserError("Image editing requires the multimodal chat image API mode.")
            response = await self.post(
                "/images/generations",
                {
                    "model": self.settings.image_model,
                    "prompt": instruction,
                    "n": 1,
                    "response_format": "b64_json",
                },
            )
            outputs = [
                decode_image(
                    f"data:{item.get('media_type', 'image/png')};base64,{item['b64_json']}"
                )
                for item in response.get("data", [])[:4]
                if item.get("b64_json")
            ]
        else:
            response = await self.post(
                "/chat/completions",
                {
                    "model": self.settings.image_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": instruction},
                                *[item.content() for item in inputs],
                            ],
                        }
                    ],
                    "modalities": ["image", "text"],
                },
            )
            outputs = self.images(self.message(response))
        if not outputs:
            raise UserError(
                "The image model returned no image. Check that it supports image output."
            )
        return outputs

    async def run(self, prompt: Prompt) -> Answer:
        if not self.settings.model:
            raise UserError("Choose a chat model in Connection settings first.")
        messages = [
            {"role": "system", "content": self.settings.system_prompt},
            {"role": "user", "content": prompt.content()},
        ]
        answer = Answer()
        tools_enabled = self.settings.image_tools and bool(self.settings.image_model)
        for step in range(self.settings.max_tool_rounds + 1):
            payload = {
                "model": self.settings.model,
                "messages": messages,
                "temperature": self.settings.temperature,
                "max_tokens": self.settings.max_tokens,
            }
            if tools_enabled:
                payload["tools"] = [IMAGE_TOOL]
                payload["tool_choice"] = "none" if step == self.settings.max_tool_rounds else "auto"
                payload["parallel_tool_calls"] = False
            message = self.message(await self.post("/chat/completions", payload))
            calls = message.get("tool_calls") or []
            if not calls:
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = "\n".join(
                        p.get("text", "") for p in content if p.get("type") == "text"
                    )
                answer.text = content
                answer.media.extend(self.images(message))
                if not answer.text and not answer.media:
                    raise UserError("The AI provider returned an empty answer.")
                return answer
            if not tools_enabled or step == self.settings.max_tool_rounds or len(calls) > 4:
                raise UserError("The agent reached its tool limit. Try a more specific request.")
            messages.append(
                {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
            )
            for call in calls:
                try:
                    function = call["function"]
                    if function["name"] != "create_or_edit_image":
                        raise UserError("Unknown tool requested.")
                    args = json.loads(function["arguments"])
                    if not isinstance(args.get("prompt"), str) or not args["prompt"].strip():
                        raise UserError("An image instruction is required.")
                    if not isinstance(args.get("use_input_images"), bool):
                        raise UserError("use_input_images must be a boolean.")
                    inputs = (
                        [m for m in prompt.media if m.mime.startswith("image/")]
                        if args["use_input_images"]
                        else []
                    )
                    if args["use_input_images"] and not inputs:
                        raise UserError("No image was attached to edit.")
                    if answer.tool_calls >= self.settings.max_tool_rounds:
                        raise UserError("Image tool call budget reached.")
                    answer.tool_calls += 1
                    outputs = await self.image(args["prompt"], inputs)
                    answer.media.extend(outputs)
                    result = f"Success: {len(outputs)} image(s) created and queued for delivery."
                except (KeyError, ValueError, TypeError, UserError) as exc:
                    result = str(exc) if isinstance(exc, UserError) else "Invalid tool arguments."
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        raise UserError("The agent could not finish within its tool budget.")
