"""Teste manual da IA rodando na sua máquina (não faz parte da suíte pytest).

Faz login no Spring e conversa com a IA em http://127.0.0.1:8000.
Pedidos de alteração (ex.: "dê saída de 5 kg") são só validados: a IA responde que nada foi
alterado, porque o human in the loop ainda não foi implementado.

Exemplos (na pasta da IA, com a IA já rodando em outro terminal):
    .\\.venv\\Scripts\\python.exe scripts/testar_local.py
    .\\.venv\\Scripts\\python.exe scripts/testar_local.py "Quanto tenho de picanha?"
    .\\.venv\\Scripts\\python.exe scripts/testar_local.py "Dê saída de 5 kg de picanha"
"""

import argparse
import sys
import time
import uuid

import httpx

PERGUNTAS_PADRAO = [
    "O que é o OCR?",
    "Quanto tenho de picanha em estoque?",
    "Quais produtos vencem nos próximos 7 dias?",
    "Dê saída de 5 kg de picanha.",
]


def mostrar_erro(resposta: httpx.Response) -> None:
    print(f"   !! HTTP {resposta.status_code}: {resposta.text[:600]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ia", default="http://127.0.0.1:8000")
    ap.add_argument("--spring", default="https://ms-inventra-api.onrender.com/api")
    ap.add_argument("--email", default="davi@inventra.com")
    ap.add_argument("--senha", default="inventra@123")
    ap.add_argument(
        "--mock",
        action="store_true",
        help="IA em modo mock: usa a identidade demo-supervisor, sem Spring",
    )
    ap.add_argument("perguntas", nargs="*", help="substitui as perguntas padrão")
    args = ap.parse_args()

    with httpx.Client(timeout=180) as http:
        if args.mock:
            token, user_id = "demo-supervisor", "demo-supervisor"
        else:
            login = http.post(
                f"{args.spring}/auth/login",
                json={"email": args.email, "password": args.senha},
            )
            if login.status_code != 200:
                print("Login no Spring falhou:")
                mostrar_erro(login)
                return 1
            dados = login.json()
            token, user_id = dados["token"], dados["user"]["id"]
            print(f"Login ok: {args.email} (id {user_id})")
        cabecalho = {"Authorization": f"Bearer {token}"}
        sessao = f"teste-local-{uuid.uuid4().hex[:8]}"

        for pergunta in args.perguntas or PERGUNTAS_PADRAO:
            print(f"\n> {pergunta}")
            inicio = time.time()
            resposta = http.post(
                f"{args.ia}/chat",
                headers=cabecalho,
                json={
                    "session_id": sessao,
                    "user_id": user_id,
                    "pergunta": pergunta,
                },
            )
            segundos = time.time() - inicio
            if resposta.status_code != 200:
                mostrar_erro(resposta)
                continue
            print(f"({segundos:.0f}s)\n{resposta.json()['resposta']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
