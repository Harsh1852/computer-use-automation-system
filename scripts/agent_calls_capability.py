"""An agent discovers a capability by name and calls it.

This is the through-line closing on itself, and it is worth being precise
about what happens here:

* The model is given the catalog's `GET /tools` output verbatim as its
  `tools` array. It has never seen the application, the artifact, or a
  locator.
* It picks a capability and supplies typed arguments, from the declared
  contract alone.
* The invocation runs a **deterministic replay** — no model inside it.
* The three-arm result goes back to the model, which reads the answer.

The interesting case is the second goal below: the model is asked about a
member that does not exist, gets `MEMBER_NOT_FOUND`, and reports it as an
answer. That is only possible because the outcome is declared in the contract
and therefore in the tool description, so the model knows in advance that
"no such member" is a legitimate reply and not something to retry.

    make agent-demo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

GOALS = [
    "What is the current savings balance for member 10001?",
    "And for member 99999?",
]

SYSTEM = """\
You are a banking operations assistant. You have tools that operate the \
institution's back-office system. Use them to answer the user; do not guess \
a number you have not retrieved.

Some tools document legitimate outcomes other than success - for example a \
member that does not exist. Those are answers, not errors. Report them as \
answers and do not retry the call.\
"""


async def main(base_url: str, tenant: str | None) -> int:
    import httpx
    from openai import AsyncOpenAI

    model = os.environ.get("OPENAI_MODEL")
    if not model or not os.environ.get("OPENAI_API_KEY"):
        print("needs OPENAI_API_KEY and OPENAI_MODEL", file=sys.stderr)
        return 2

    params = {"tenant_id": tenant} if tenant else {}
    async with httpx.AsyncClient(base_url=base_url, timeout=300) as http:
        tools = (await http.get("/tools", params=params)).json()
        print(f"discovered {len(tools)} capabilities from {base_url}/tools:")
        for tool in tools:
            print(f"  - {tool['function']['name']}")
        print()

        client = AsyncOpenAI()
        messages: list[dict] = [{"role": "system", "content": SYSTEM}]

        for goal in GOALS:
            print(f"USER: {goal}")
            messages.append({"role": "user", "content": goal})

            while True:
                response = await client.chat.completions.create(
                    model=model, messages=messages, tools=tools, tool_choice="auto"
                )
                choice = response.choices[0].message
                messages.append(choice.model_dump(exclude_none=True))

                if not choice.tool_calls:
                    print(f"AGENT: {choice.content}\n")
                    break

                for call in choice.tool_calls:
                    name = call.function.name
                    args = json.loads(call.function.arguments or "{}")
                    print(f"  -> invoking {name}({args})")

                    reply = await http.post(
                        f"/capabilities/{name}/invoke",
                        json={"params": args, **params},
                    )
                    result = reply.json()
                    summary = {
                        "status": result.get("status"),
                        "outputs": result.get("outputs"),
                        "code": result.get("code"),
                        "message": result.get("message"),
                        "kind": result.get("kind"),
                        "observed": result.get("observed"),
                    }
                    summary = {k: v for k, v in summary.items() if v is not None}
                    print(f"  <- {json.dumps(summary)}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(summary),
                        }
                    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default="http://localhost:8081")
    parser.add_argument("--tenant", default=None)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.catalog, args.tenant)))
