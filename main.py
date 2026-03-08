#!uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "flet[all]>=0.80.5",
#     "duckdb",
#     "cryptography",
#     "python-dotenv",
#     "httpx",
# ]
# ///

import asyncio
import os
import json
import duckdb
from dataclasses import field
from typing import Callable, Any, Tuple

import flet as ft
import httpx
from cryptography.fernet import Fernet
from dotenv import load_dotenv

STORAGE_KEY = "todo_app.tasks"


# -------------------------
# Encriptação Fernet
# -------------------------

load_dotenv()
FERNET_KEY = os.getenv("FERNET_KEY")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY não definida no .env")

fernet = Fernet(FERNET_KEY.encode())
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
if not GITHUB_CLIENT_ID:
    raise RuntimeError("GITHUB_CLIENT_ID não definido no .env")

GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"


def encrypt_text(plain: str) -> str:
    return fernet.encrypt(plain.encode()).decode()


def decrypt_text(token: str) -> str:
    try:
        return fernet.decrypt(token.encode()).decode()
    except Exception:
        # Dados antigos que ainda não estavam encriptados
        return token


async def github_device_login(page: ft.Page) -> Tuple[str, str]:
    """Fluxo OAuth2 Device Code com GitHub. Devolve (user_id, username)."""

    # 0) Verificar se já existe uma sessão gravada localmente
    saved_user_id = await page.shared_preferences.get("github_user_id")
    saved_username = await page.shared_preferences.get("github_username")
    
    if saved_user_id and saved_username:
        # Mostra brevemente uma mensagem apenas para dar feedback visual
        welcome_text = ft.Text(f"Sessão restaurada: Bem-vindo de volta, {saved_username}!", size=16, color=ft.Colors.GREEN)
        page.add(welcome_text)
        page.update()
        await asyncio.sleep(1.5)
        page.controls.remove(welcome_text)
        page.update()
        return saved_user_id, saved_username

    status = ft.Text("A autenticar com GitHub...", size=16)
    code_text = ft.Text(size=20, weight=ft.FontWeight.BOLD, selectable=True)
    url_text = ft.Text(selectable=True)

    page.add(status, code_text, url_text)
    page.update()

    async with httpx.AsyncClient() as client:
        # 1) Pedir device_code
        resp = await client.post(
            GITHUB_DEVICE_CODE_URL,
            data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_uri = data["verification_uri"]
        interval = data.get("interval", 5)

        status.value = "1) Autenticação necessária"
        code_text.value = f"Código GitHub: {user_code}"
        url_text.value = f"Abrir: {verification_uri}"
        page.update()

        try:
            # await to fix coroutine not awaited warning and actually execute the redirect
            await page.launch_url_async(verification_uri)
        except Exception:
            try:
                await page.launch_url(verification_uri)
            except Exception:
                pass

        # 2) Poll para obter access_token
        status.value = "2) Autoriza a app no GitHub e espera..."
        page.update()

        access_token = None
        while access_token is None:
            await asyncio.sleep(interval)
            token_resp = await client.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )
            token_data = token_resp.json()

            if "error" in token_data:
                if token_data["error"] in ("authorization_pending", "slow_down"):
                    # continua a esperar
                    continue
                raise RuntimeError(f"Erro OAuth GitHub: {token_data['error']}")

            access_token = token_data.get("access_token")

        # 3) Obter dados do utilizador
        user_resp = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=10,
        )
        user_resp.raise_for_status()
        user = user_resp.json()

        user_id = str(user["id"])
        username = user.get("login", "")

        status.value = f"Autenticado como {username} (GitHub)"
        code_text.value = ""
        url_text.value = ""
        page.update()

        # Guardar localmente (cache persistente Flet Web/Desktop)
        await page.shared_preferences.set("github_user_id", user_id)
        await page.shared_preferences.set("github_username", username)

        return user_id, username

# Diretório persistente (nunca é limpo): %APPDATA%/todo_app no Windows
# Evita que o Parquet fique em pastas temp quando o Flet web corre via uv/cache
def _data_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, "todo_app")
    os.makedirs(path, exist_ok=True)
    return path

DATA_DIR = _data_dir()
PARQUET_PATH = os.path.join(DATA_DIR, "tasks.parquet")
PARQUET_PATH_SQL = PARQUET_PATH.replace("\\", "/")  # DuckDB aceita / no Windows


# -------------------------
# Task Component
# -------------------------

@ft.control
class Task(ft.Column):
    task_name: str = ""
    completed: bool = False
    on_change: Callable[[], Any] = field(default=None)
    on_delete: Callable[["Task"], Any] = field(default=None)


    def init(self):
        self.display_task = ft.Checkbox(
            value=self.completed,
            label=self.task_name,
            on_change=self.status_changed,
        )

        self.edit_name = ft.TextField(expand=True)

        self.display_view = ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            
            controls=[
                self.display_task,
                ft.Row(
                    spacing=0,
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.CREATE_OUTLINED,
                            tooltip="Edit",
                            on_click=self.edit_clicked,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            tooltip="Delete",
                            on_click=self.delete_clicked,
                        ),
                    ],
                ),
            ],
        )

        self.edit_view = ft.Row(
            visible=False,
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                self.edit_name,
                ft.IconButton(
                    icon=ft.Icons.DONE,
                    icon_color=ft.Colors.GREEN,
                    tooltip="Save",
                    on_click=self.save_clicked,
                ),
            ],
        )

        self.controls = [self.display_view, self.edit_view]

    async def edit_clicked(self, e):
        self.edit_name.value = self.display_task.label
        self.display_view.visible = False
        self.edit_view.visible = True
        self.update()

    async def save_clicked(self, e):
        self.task_name = self.edit_name.value
        self.display_task.label = self.task_name
        self.display_view.visible = True
        self.edit_view.visible = False
        self.update()

        if self.on_change:
            await self.on_change()

    async def status_changed(self, e):
        self.completed = self.display_task.value
        self.update()

        if self.on_change:
            await self.on_change()

    async def delete_clicked(self, e):
        if self.on_delete:
            await self.on_delete(self)


# -------------------------
# Todo App
# -------------------------

@ft.control
class TodoApp(ft.Column):
    user_id: str = ""

    def init(self):
        self.new_task = ft.TextField(
            hint_text="What needs to be done?",
            expand=True,
        )

        self.tasks = ft.Column(spacing=10)

        self.filter_status = "all"

        self.filter_bar = ft.SegmentedButton(
            selected=["all"],
            allow_multiple_selection=False,
            show_selected_icon=False,
            segments=[
                ft.Segment(value="all", label=ft.Text("all")),
                ft.Segment(value="active", label=ft.Text("active")),
                ft.Segment(value="completed", label=ft.Text("completed")),
            ],
            on_change=self.filter_changed,
        )

        self.width = 600
        self.controls = [
            ft.Row(
                controls=[
                    self.new_task,
                    ft.FloatingActionButton(
                        icon=ft.Icons.ADD,
                        on_click=self.add_clicked,
                    ),
                ],
            ),
            ft.Column(
                spacing=20,
                controls=[
                    ft.Row([self.filter_bar], alignment=ft.MainAxisAlignment.CENTER),
                    self.tasks,
                ],
            ),
        ]

    def did_mount(self):
        self.page.run_task(self.load_tasks)
        self.page.run_task(self._sync_poll_loop)

    async def _sync_poll_loop(self):
        """Sincroniza com o Parquet a cada 5s para ver alterações feitas noutros clientes."""
        while True:
            await asyncio.sleep(5)
            if not os.path.exists(PARQUET_PATH):
                continue
            try:
                conn = duckdb.connect()
                rows = conn.execute(
                    f"""
                    SELECT name, completed
                    FROM read_parquet('{PARQUET_PATH_SQL}')
                    WHERE user_id = ?
                    """,
                    [self.user_id],
                ).fetchall()
                conn.close()
                data = [
                    (decrypt_text(str(name)), bool(completed))
                    for name, completed in rows
                ]
            except Exception:
                continue
            current = self._tasks_to_data()
            if data != current:
                self._data_to_tasks(data)
                self.update()

    def _tasks_to_data(self):
        return [(t.task_name, t.completed) for t in self.tasks.controls]

    def _data_to_tasks(self, data):
        self.tasks.controls.clear()
        for name, completed in data:
            task = Task(
                task_name=name,
                completed=completed,
                on_change=self.save_tasks,
                on_delete=self.task_delete,
            )
            self.tasks.controls.append(task)

    async def load_tasks(self):
        data = []

        if os.path.exists(PARQUET_PATH):
            try:
                conn = duckdb.connect()
                rows = conn.execute(
                    f"""
                    SELECT name, completed
                    FROM read_parquet('{PARQUET_PATH_SQL}')
                    WHERE user_id = ?
                    """,
                    [self.user_id],
                ).fetchall()
                conn.close()
                data = [
                    (decrypt_text(str(name)), bool(completed))
                    for name, completed in rows
                ]
            except Exception:
                pass

        if not data and self.page and hasattr(self.page, "shared_preferences"):
            try:
                key = f"{STORAGE_KEY}.{self.user_id}"
                stored = await self.page.shared_preferences.get(key)
                if stored:
                    decoded = json.loads(stored)
                    data = [
                        (decrypt_text(t["name"]), t["completed"]) for t in decoded
                    ]
            except Exception:
                pass

        if data:
            self._data_to_tasks(data)
            if self.page and hasattr(self.page, "shared_preferences"):
                try:
                    stored = [
                        {"name": encrypt_text(n), "completed": c} for n, c in data
                    ]
                    key = f"{STORAGE_KEY}.{self.user_id}"
                    await self.page.shared_preferences.set(
                        key, json.dumps(stored)
                    )
                except Exception:
                    pass
        self.update()

    async def save_tasks(self):
        tasks_data = self._tasks_to_data()

        # Encripta o nome da tarefa para armazenamento persistente
        encrypted_tasks = [(encrypt_text(name), completed) for name, completed in tasks_data]

        try:
            conn = duckdb.connect()
            conn.execute(
                "CREATE OR REPLACE TABLE temp_tasks (user_id VARCHAR, name VARCHAR, completed BOOLEAN)"
            )
            for name, completed in encrypted_tasks:
                conn.execute(
                    "INSERT INTO temp_tasks VALUES (?, ?, ?)",
                    [self.user_id, name, completed],
                )
            conn.execute(f"COPY temp_tasks TO '{PARQUET_PATH_SQL}' (FORMAT parquet)")
            conn.close()
        except Exception:
            pass

        if self.page and hasattr(self.page, "shared_preferences"):
            try:
                stored = [
                    {"name": encrypt_text(n), "completed": c} for n, c in tasks_data
                ]
                key = f"{STORAGE_KEY}.{self.user_id}"
                await self.page.shared_preferences.set(
                    key, json.dumps(stored)
                )
            except Exception:
                pass

    async def add_clicked(self, e):
        if not self.new_task.value:
            return

        task = Task(
            task_name=self.new_task.value,
            completed=False,
            on_change=self.save_tasks,
            on_delete=self.task_delete,
        )

        self.tasks.controls.append(task)
        self.new_task.value = ""

        await self.save_tasks()
        self.update()

    async def task_delete(self, task):
        self.tasks.controls.remove(task)
        await self.save_tasks()
        self.update()

    async def filter_changed(self, e):
        selected = self.filter_bar.selected
        self.filter_status = selected[0] if selected else "all"
        
        # Atualiza apenas a propriedade visible de cada tarefa individualmente
        status = self.filter_status
        for task in self.tasks.controls:
            task.visible = (
                status == "all"
                or (status == "active" and not task.completed)
                or (status == "completed" and task.completed)
            )
            
        # Pede ao Flet para atualizar APENAS a lista de tarefas, 
        # ignorando o resto da página, poupando bastante performance
        self.tasks.update()


# -------------------------
# Main
# -------------------------

async def main(page: ft.Page):
    page.title = "To-Do App"
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.scroll = ft.ScrollMode.ADAPTIVE  
    page.padding = ft.Padding.only(left=20, top=80, right=20, bottom=80)

    # Autenticação OAuth2 (GitHub) antes de mostrar o gestor de tarefas
    user_id, username = await github_device_login(page)

    page.clean()  # Limpa os controlos do login
    app = TodoApp(user_id=user_id)
    page.add(app)


ft.run(main)