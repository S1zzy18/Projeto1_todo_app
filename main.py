#!uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "flet[all]>=0.80.5",
#     "duckdb",
# ]
# ///

import asyncio
import os
import duckdb
from dataclasses import field
from typing import Callable, Any

import flet as ft

STORAGE_KEY = "todo_tasks"

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
    def init(self):
        self.new_task = ft.TextField(
            hint_text="What needs to be done?",
            expand=True,
        )

        self.tasks = ft.Column()

        self.filter_status = "all"

        self.filter_bar = ft.SegmentedButton(
            selected=["all"],
            allow_multiple_selection=False,
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
                    self.filter_bar,
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
                    f"SELECT name, completed FROM read_parquet('{PARQUET_PATH_SQL}')"
                ).fetchall()
                conn.close()
                data = [(str(name), bool(completed)) for name, completed in rows]
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
                    f"SELECT name, completed FROM read_parquet('{PARQUET_PATH_SQL}')"
                ).fetchall()
                conn.close()
                data = [(str(name), bool(completed)) for name, completed in rows]
            except Exception:
                pass

        if not data and self.page and hasattr(self.page, "client_storage"):
            try:
                stored = await self.page.client_storage.get(STORAGE_KEY)
                if stored:
                    data = [(t["name"], t["completed"]) for t in stored]
            except Exception:
                pass

        if data:
            self._data_to_tasks(data)
            if self.page and hasattr(self.page, "client_storage"):
                try:
                    stored = [{"name": n, "completed": c} for n, c in data]
                    await self.page.client_storage.set(STORAGE_KEY, stored)
                except Exception:
                    pass
        self.update()

    async def save_tasks(self):
        tasks_data = self._tasks_to_data()

        try:
            conn = duckdb.connect()
            conn.execute(
                "CREATE OR REPLACE TABLE temp_tasks (name VARCHAR, completed BOOLEAN)"
            )
            for name, completed in tasks_data:
                conn.execute(
                    "INSERT INTO temp_tasks VALUES (?, ?)",
                    [name, completed],
                )
            conn.execute(f"COPY temp_tasks TO '{PARQUET_PATH_SQL}' (FORMAT parquet)")
            conn.close()
        except Exception:
            pass

        if self.page and hasattr(self.page, "client_storage"):
            try:
                stored = [{"name": n, "completed": c} for n, c in tasks_data]
                await self.page.client_storage.set(STORAGE_KEY, stored)
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
        self.update()

    def before_update(self):
        status = self.filter_status
        for task in self.tasks.controls:
            task.visible = (
                status == "all"
                or (status == "active" and not task.completed)
                or (status == "completed" and task.completed)
            )


# -------------------------
# Main
# -------------------------

async def main(page: ft.Page):
    page.title = "To-Do App"
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.padding = ft.Padding.only(left=20, top=80, right=20)

    app = TodoApp()
    page.add(app)


ft.run(main)