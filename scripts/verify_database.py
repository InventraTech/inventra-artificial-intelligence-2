"""Opt-in read-only smoke test against the configured real schema. No writes/DDL."""
import sys
from pathlib import Path

from psycopg2 import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iai.app.database import transaction
from iai.app.domain import ReadQuery
from iai.app.repository import VIEWS, Repository


def main():
    with transaction() as conn:
        repo = Repository(conn)
        assert repo.one("SHOW transaction_read_only")["transaction_read_only"] == "on"
        kitchen = repo.one("SELECT id_kitchen FROM public.tb_kitchen WHERE active ORDER BY id_kitchen LIMIT 1")
        if kitchen is None:
            raise RuntimeError("Nenhuma cozinha ativa disponível para a verificação de leitura.")
        for resource in [*VIEWS, "produtos", "itens_requisicao", "saldo_categoria", "tendencia_categoria"]:
            kitchen_id = kitchen["id_kitchen"]
            if resource in VIEWS and resource not in {"catalogo", "fornecedores"}:
                candidate = repo.one(sql.SQL("SELECT id_kitchen FROM public.{} WHERE id_kitchen IS NOT NULL LIMIT 1").format(sql.Identifier(VIEWS[resource][0])))
                if candidate:
                    kitchen_id = candidate["id_kitchen"]
            rows = repo.read(ReadQuery(resource=resource, limit=2), kitchen_id)
            assert all(row.get("id_kitchen", kitchen_id) == kitchen_id for row in rows)
            print(f"{resource}: OK ({min(len(rows), 2)} registros, limite 2)")
        for role in ("ESTOQUISTA", "COMPRADOR", "SUPERVISOR"):
            user = repo.one("""SELECT u.id_user::text AS id_user FROM public.tb_user u
              JOIN public.tb_profile p USING (id_profile) JOIN public.tb_kitchen k USING (id_kitchen)
              WHERE u.active AND k.active AND split_part(p.access_type,'_',1)=%s LIMIT 1""", (role,))
            if user:
                assert repo.principal(user["id_user"])
                print(f"perfil {role}: OK")
            else:
                print(f"perfil {role}: sem usuário ativo para verificar")
        batch = repo.one("SELECT id_batch,id_product,id_kitchen,id_supplier FROM public.tb_stock_batch ORDER BY id_batch LIMIT 1")
        if batch:
            assert repo.product(batch["id_product"])
            assert repo.batches(batch["id_product"],batch["id_kitchen"],batch["id_batch"])
            print("produto e lote por ID: OK")
        print("Verificação somente leitura concluída; nenhuma alteração de dados/schema.")


if __name__ == "__main__":
    main()
