"""Auth routes -- placeholder until the accounts package lands.

Holds only ``POST /logout`` so the app boots and the nav partial's sign-out
form resolves. The full signup / login / verify / reset routes replace this
file.
"""
from flask import Blueprint, redirect

from utils.identity import logout_user

bp = Blueprint('auth', __name__)


@bp.route('/logout', methods=['POST'])
def logout():
    logout_user()
    return redirect('/')
