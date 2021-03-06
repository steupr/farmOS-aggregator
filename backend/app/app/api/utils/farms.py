import os
import logging
from typing import List
import time

import requests
from fastapi import Query, Depends, HTTPException
from starlette.status import HTTP_403_FORBIDDEN, HTTP_401_UNAUTHORIZED
from sqlalchemy.orm import Session
from functools import partial
from farmOS import farmOS
from farmOS.config import ClientConfig

from app import crud
from app.api.utils.db import get_db
from app.core.config import settings
from app.schemas.farm_token import FarmTokenBase, FarmTokenCreate
from app.crud.farm_token import create_farm_token, update_farm_token
from app.schemas.farm import Farm, FarmUpdate
from app.schemas.token import FarmAccess
from app.api.utils.security import get_farm_access
from app.utils import send_admin_alert_email


logger = logging.getLogger(__name__)

unauthorized_exception = HTTPException(
    status_code = HTTP_401_UNAUTHORIZED,
    detail="Not enough permissions to access this farm."
)

farm_not_found_exception = HTTPException(
    status_code = 404,
    detail="Farm does not exist."
)


def get_farm_by_url(
    db: Session = Depends(get_db),
    farm_url: str = Query(None),
    farm_access: FarmAccess = Depends(get_farm_access)
):
    farm = None
    if farm_url is not None:
        farm = crud.farm.get_by_url(db, farm_url=farm_url)

        if farm is None:
            raise farm_not_found_exception

        if not farm_access.can_access_farm(farm.id):
            raise unauthorized_exception

    return farm


def get_active_farm_by_url(
    db: Session = Depends(get_db),
    farm_url: str = Query(None),
    farm_access: FarmAccess = Depends(get_farm_access)
):
    farm = None
    if farm_url is not None:
        farm = crud.farm.get_by_url(db, farm_url=farm_url, active=True)

        if farm is None:
            raise farm_not_found_exception

        if not farm_access.can_access_farm(farm.id):
            raise unauthorized_exception

    return farm


def get_farms_by_id_list(
    db: Session = Depends(get_db),
    farm_id: List[int] = Query(None),
    farm_access: FarmAccess = Depends(get_farm_access)
):
    # Load all farms if the user can access all farms.
    if farm_id is None and farm_access.all_farms:
        farms = crud.farm.get_multi(db)
        return farms

    # Load all the farms the user has access to if none are provided.
    if farm_id is None and farm_access.farm_id_list is not None:
        farms = crud.farm.get_by_multi_id(db, farm_id_list=farm_access.farm_id_list)
        return farms

    # Load the requested farm(s) if the user has access.
    if farm_id is not None:
        for id in farm_id:
            if not farm_access.can_access_farm(id):
                raise unauthorized_exception

        farms_by_id = crud.farm.get_by_multi_id(db, farm_id_list=farm_id)

        if len(farms_by_id) > 0:
            return farms_by_id
        else:
            raise farm_not_found_exception


def get_active_farms_by_id_list(
    db: Session = Depends(get_db),
    farm_id: List[int] = Query(None),
    farm_access: FarmAccess = Depends(get_farm_access),
):
    # Load all farms if the user can access all farms.
    if farm_id is None and farm_access.all_farms:
        farms = crud.farm.get_multi(db, active=True)
        return farms

    # Load all the farms the user has access to if none are provided.
    if farm_id is None and farm_access.farm_id_list is not None:
        farms = crud.farm.get_by_multi_id(db, farm_id_list=farm_access.farm_id_list, active=True)
        return farms

    # Load the requested farm(s) if the user has access.
    if farm_id is not None:
        for id in farm_id:
            if not farm_access.can_access_farm(id):
                raise unauthorized_exception

        farms_by_id = crud.farm.get_by_multi_id(db, farm_id_list=farm_id, active=True)

        if len(farms_by_id) > 0:
            return farms_by_id
        else:
            raise farm_not_found_exception


def get_farm_by_id(
    farm_id: int,
    db: Session = Depends(get_db),
    farm_access: FarmAccess = Depends(get_farm_access)
):
    if not farm_access.can_access_farm(farm_id):
        raise unauthorized_exception

    farm = crud.farm.get_by_id(db, farm_id=farm_id)

    if not farm:
        raise farm_not_found_exception

    return farm


def get_farms_url_or_list(
    farm_by_url: Farm = Depends(get_farm_by_url),
    farms_by_list: List[Farm] = Depends(get_farms_by_id_list),
):
    farms = []

    # Give priority to a farm requested by URL
    # to avoid returning the same farm twice
    if farm_by_url is not None:
        farms.append(farm_by_url)
    elif farms_by_list is not None:
        farms.extend(farms_by_list)

    return farms


def get_active_farms_url_or_list(
    farm_by_url: Farm = Depends(get_active_farm_by_url),
    farms_by_list: List[Farm] = Depends(get_active_farms_by_id_list),
):
    farms = []

    # Give priority to a farm requested by URL
    # to avoid returning the same farm twice
    if farm_by_url is not None:
        farms.append(farm_by_url)
    elif farms_by_list is not None:
        farms.extend(farms_by_list)

    return farms


# A helper function to save OAuth Tokens to DB.
def _save_token(token, db_session=None, farm=None):
    logging.debug("Saving new token for farm: " + str(farm.id))
    token_in = FarmTokenCreate(farm_id=farm.id, **token)

    # Make sure we have a DB session and Farm object.
    if db_session is not None and farm is not None:
        # Update the farm token if it exists.
        if farm.token is not None:
            update_farm_token(db_session, farm.token, token_in)
        else:
            create_farm_token(db_session, token_in)


# Create a farmOS.py client.
def get_farm_client(db_session, farm):
    client_id = settings.AGGREGATOR_OAUTH_CLIENT_ID
    client_secret = settings.AGGREGATOR_OAUTH_CLIENT_SECRET

    scope = None
    if farm.scope is not None:
        scope = farm.scope

    token = FarmTokenBase.from_orm(farm.token)

    token_updater = partial(_save_token, db_session=db_session, farm=farm)

    # Allow OAuth over http
    if settings.AGGREGATOR_OAUTH_INSECURE_TRANSPORT:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

    try:
        client = farmOS(
            hostname=farm.url,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            token=token.dict(),
            token_updater=token_updater
        )
        crud.farm.update_last_accessed(db_session, farm_id=farm.id)
        crud.farm.update_is_authorized(db_session, farm_id=farm.id, is_authorized=True)
    except Exception as e:
        if settings.AGGREGATOR_ALERT_ALL_ERRORS:
            admin_alert_email(db_session=db_session, message="Cannot authenticate client with farmOS server id: " + str(farm.id) + " - " + repr(e) + str(e))
        logging.error("Cannot authenticate client with farmOS server id: " + str(farm.id) + " - " + repr(e) + str(e))
        crud.farm.update_is_authorized(db_session, farm_id=farm.id, is_authorized=False, auth_error=str(e))
        raise ClientError(e)

    return client


def get_oauth_token(farm_url, auth_params):
    logging.debug("Completing Authorization Code flow for: " + farm_url)
    data = {}
    data['code'] = auth_params.code
    data['state'] = auth_params.state
    data['grant_type'] = auth_params.grant_type
    data['client_id'] = auth_params.client_id
    data['redirect_uri'] = farm_url + "/api/authorized"

    if auth_params.client_secret is not None:
        data['client_secret'] = auth_params.client_secret

    if auth_params.redirect_uri is not None:
        data['redirect_uri'] = auth_params.redirect_uri

    token_url = farm_url + "/oauth2/token"

    response = requests.post(token_url, data)

    if response.status_code == 200:
        response_token = response.json()
        logging.debug("Successfully retrieved access token")

        if "expires_at" not in response_token:
            response_token['expires_at'] = str(time.time() + int(response_token['expires_in']))

        new_token = FarmTokenBase(**response_token)
        return new_token
    else:
        logging.error("Could not complete OAuth Authorization Flow: " )
        raise HTTPException(status_code=400, detail="Could not retrieve an access token.")


def admin_alert_email(db_session, message: str):
    if settings.EMAILS_ENABLED:
        logging.info("Sending admin alert message: " + message)
        users = crud.user.get_multi(db_session)

        for user in users:
            if user.is_superuser:
                send_admin_alert_email(email_to=user.email, message=message)


class ClientError(Exception):
    def __init__(self, message):
        self.message = message