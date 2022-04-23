import boto3
import os
import logging

logger = logging.getLogger('shared')
logger.setLevel(logging.INFO)

iam = boto3.client('iam', region_name=os.environ.get('AWS_REGION'))

def fetch_account_info():
    return {
        'id': os.environ['AWS_ACCOUNT_ID'],
        'name': iam.list_account_aliases()['AccountAliases'][0]
    }