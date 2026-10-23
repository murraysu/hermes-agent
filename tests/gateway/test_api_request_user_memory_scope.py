"""Local patch: the OpenAI ``user`` field scopes an API turn's memory on the session-authority path.

channel_gw sends ``user: <employee id>`` so each employee gets their own Honcho peer. The authority
path builds the agent from the session source, whose user is the shared ``'api'``; the request user
rides the admission instead and overrides only the agent's ``user_id``.
"""


def test_request_user_rides_the_admission_to_execution(api, owner):
    from gateway.session_api_turn import admit_api_turn, prepare_api_execution
    _, ref, row = admit_api_turn(api, session_id='scoped', user_message='x', conversation_history=[],
                                 user_id='employee-42')
    assert row['payload']['api_turn_v1']['user_id'] == 'employee-42'
    assert prepare_api_execution(owner, ref, row['payload'])['user_id'] == 'employee-42'
    # Routing and ownership still use the shared API source user.
    assert owner.sessions[ref.session_id].source.user_id == 'api'


def test_no_request_user_keeps_the_source_user(api, owner):
    from gateway.session_api_turn import admit_api_turn, prepare_api_execution
    _, ref, row = admit_api_turn(api, session_id='unscoped', user_message='x', conversation_history=[])
    assert 'user_id' not in row['payload']['api_turn_v1']
    assert prepare_api_execution(owner, ref, row['payload'])['user_id'] is None


def test_agent_build_prefers_the_admitted_user():
    from gateway.session_api_turn import api_execution
    from gateway.session_execution import _api_turn_user_id
    assert _api_turn_user_id() is None
    token = api_execution.set({'user_id': 'employee-42'})
    try:
        assert _api_turn_user_id() == 'employee-42'
    finally:
        api_execution.reset(token)
