"""Admin routes — user management panel."""

from fasthtml.common import (
    Button, Div, Form, H2, Input, Label, Option, P, Select, Span,
    Table, Tbody, Td, Th, Thead, Tr,
)
from starlette.requests import Request
from starlette.responses import Response

from nctqai.components import TABLE_CLS
from nctqai.layout import Layout
from nctqai.models.auth import Role
from nctqai.routes._auth import require_section
from nctqai.services.admin import count_active_admins, create_user, get_all_users, update_user


# Single source: nctqai.models.auth.Role. Order = enum definition order
# (viewer, analyst, power_user, admin) — matches the invite/edit dropdowns.
VALID_ROLES = [r.value for r in Role]


def register_admin_routes(rt):

    @rt("/admin/users")
    def get_admin_users(request: Request, msg: str = ""):
        """Admin user list with invite form and edit controls."""
        user, deny = require_section(request, "admin")
        if deny:
            return deny

        users = get_all_users()

        # Build invite form
        invite_form = Form(
            H2("Invite User", cls="text-lg mb-md"),
            Div(
                Div(
                    Label("Email", htmlfor="invite_email"),
                    Input(type="email", name="email", id="invite_email", required=True,
                          cls="uk-input uk-form-small", placeholder="user@example.com"),
                    cls="flex-1",
                ),
                Div(
                    Label("Name", htmlfor="invite_name"),
                    Input(type="text", name="name", id="invite_name", required=True,
                          cls="uk-input uk-form-small", placeholder="Full Name"),
                    cls="flex-1",
                ),
                Div(
                    Label("Role", htmlfor="invite_role"),
                    Select(
                        *[Option(r.replace("_", " ").title(), value=r) for r in VALID_ROLES],
                        name="role", id="invite_role",
                        cls="uk-select uk-form-small",
                    ),
                    cls="select-sm",
                ),
                Div(
                    Label(" ", cls="d-block"),  # spacer for alignment
                    Button("Invite", type="submit", cls="uk-button uk-button-primary uk-button-small"),
                ),
                cls="filter-bar",
            ),
            hx_post="/admin/users/invite",
            hx_swap="none",
            cls="review-card mb-lg",
        )

        # Message banner
        msg_div = ""
        if msg:
            is_error = msg.startswith("Error")
            msg_div = Div(
                Span(msg),
                cls=f"{'alert-error' if is_error else 'alert-success'} mb-md",
            )

        # Build user table
        table = _build_users_table(users, user.id)

        content = Div(msg_div, invite_form, table)

        return Layout(
            "User Management",
            f"{len(users)} users",
            content,
            section="admin",
            user=user,
        )

    @rt("/admin/users/invite", methods=["POST"])
    def post_invite_user(request: Request, email: str = "", name: str = "", role: str = "viewer"):
        """Create a new user."""
        if not request.headers.get("hx-request"):
            return Response("Forbidden", status_code=403)
        user = request.state.user
        if not user or not user.is_admin():
            return Response("", headers={"HX-Redirect": "/login"})

        if not email or not name:
            return Response("", headers={"HX-Redirect": "/admin/users?msg=Error:+Email+and+name+required"})

        if role not in VALID_ROLES:
            return Response("", headers={"HX-Redirect": "/admin/users?msg=Error:+Invalid+role"})

        success = create_user(email, name, role, user.id)
        if success:
            return Response("", headers={"HX-Redirect": f"/admin/users?msg=Invited+{name}"})
        return Response("", headers={"HX-Redirect": "/admin/users?msg=Error:+Could+not+create+user.+Email+may+already+exist."})

    @rt("/admin/users/{user_id:int}/edit", methods=["POST"])
    def post_edit_user(request: Request, user_id: int, role: str = "", is_active: str = ""):
        """Update user role or active status."""
        if not request.headers.get("hx-request"):
            return Response("Forbidden", status_code=403)
        admin = request.state.user
        if not admin or not admin.is_admin():
            return Response("", headers={"HX-Redirect": "/login"})

        # Safety: can't edit yourself
        if user_id == admin.id:
            return Response("", headers={"HX-Redirect": "/admin/users?msg=Error:+Cannot+edit+your+own+account"})

        # Handle role change
        if role and role in VALID_ROLES:
            # Check if this would remove the last admin
            if role != "admin":
                # Check current role of target user
                from nctqai.services.auth import get_user_by_id
                target = get_user_by_id(user_id)
                if target and target.role == "admin" and count_active_admins() <= 1:
                    return Response(
                        "", headers={"HX-Redirect": "/admin/users?msg=Error:+Cannot+remove+the+last+admin"},
                    )
            update_user(user_id, role=role)

        # Handle active/inactive toggle
        if is_active in ("true", "false"):
            new_active = is_active == "true"
            if not new_active:
                # Check if deactivating last admin
                from nctqai.services.auth import get_user_by_id
                target = get_user_by_id(user_id)
                if target and target.role == "admin" and count_active_admins() <= 1:
                    return Response(
                        "", headers={"HX-Redirect": "/admin/users?msg=Error:+Cannot+deactivate+the+last+admin"},
                    )
            update_user(user_id, is_active=new_active)

        return Response("", headers={"HX-Redirect": "/admin/users?msg=User+updated"})


def _build_users_table(users, current_admin_id):
    """Build the users table with inline edit controls."""
    if not users:
        return P("No users found.", cls="uk-text-muted uk-text-center empty-state")

    rows = []
    for u in users:
        uid = u["id"]
        is_self = uid == current_admin_id
        active = u.get("is_active", True)
        last_login = u.get("last_login", "--")
        if last_login and hasattr(last_login, "strftime"):
            last_login = last_login.strftime("%Y-%m-%d %H:%M")
        elif last_login and isinstance(last_login, str) and len(last_login) > 16:
            last_login = last_login[:16]

        # Role display or dropdown
        if is_self:
            role_cell = Span(u["role"].replace("_", " ").title(), cls="badge badge-unreviewed")
        else:
            role_cell = Form(
                Select(
                    *[Option(r.replace("_", " ").title(), value=r, selected=("selected" if (r == u["role"]) else None))
                      for r in VALID_ROLES],
                    name="role",
                    cls="uk-select uk-form-small select-sm",
                    onchange="htmx.trigger(this.form, 'submit')",
                ),
                hx_post=f"/admin/users/{uid}/edit",
                hx_swap="none",
                style="display: inline;",
            )

        # Active toggle
        if is_self:
            active_cell = Span("Active", cls="text-success text-sm")
        else:
            toggle_value = "false" if active else "true"
            toggle_label = "Deactivate" if active else "Activate"
            toggle_cls = "uk-button-danger" if active else "uk-button-primary"
            active_cell = Form(
                Input(type="hidden", name="is_active", value=toggle_value),
                Button(toggle_label, type="submit",
                       cls=f"uk-button uk-button-small {toggle_cls}"),
                hx_post=f"/admin/users/{uid}/edit",
                hx_swap="none",
                style="display: inline;",
            )

        # Status indicator
        status_indicator = (
            Span("Active", cls="badge badge-accepted") if active
            else Span("Inactive", cls="badge badge-rejected")
        )

        rows.append(
            Tr(
                Td(u.get("name", ""), cls="cell-nowrap font-medium"),
                Td(u.get("email", ""), cls="text-sm text-muted"),
                Td(role_cell),
                Td(status_indicator, cls="uk-text-center"),
                Td(str(last_login) if last_login else "--", cls="uk-text-muted cell-nowrap text-sm"),
                Td(active_cell, cls="uk-text-center"),
            )
        )

    return Table(
        Thead(
            Tr(
                Th("Name", cls="uk-table-expand"),
                Th("Email", cls="uk-table-expand"),
                Th("Role", cls="uk-table-shrink"),
                Th("Status", cls="uk-table-shrink uk-text-center"),
                Th("Last Login", cls="uk-table-shrink cell-nowrap"),
                Th("Action", cls="uk-table-shrink uk-text-center"),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )
