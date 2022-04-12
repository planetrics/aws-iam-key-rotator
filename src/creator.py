import boto3
import os
import logging
import concurrent.futures
import pytz

from datetime import datetime, date
from botocore.exceptions import ClientError

# Local import
import shared

# No. of days to wait before existing key pair is deleted once a new key pair is generated
DAYS_FOR_DELETION = os.environ.get('DAYS_FOR_DELETION', 10)

# Table name which holds existing access key pair details to be deleted
IAM_KEY_ROTATOR_TABLE = os.environ.get('IAM_KEY_ROTATOR_TABLE', None)

# Days after which a new access key pair should be generated
ACCESS_KEY_AGE = os.environ.get('ACCESS_KEY_AGE', 80)

# Mail client to use for sending new key creation or existing key deletion mail
MAIL_CLIENT = os.environ.get('MAIL_CLIENT', 'ses')

# From address to be used while sending mail
MAIL_FROM = os.environ.get('MAIL_FROM', None)

# AWS_REGION environment variable is by default available within lambda environment
iam = boto3.client('iam', region_name=os.environ.get('AWS_REGION'))
dynamodb = boto3.client('dynamodb', region_name=os.environ.get('AWS_REGION'))

logger = logging.getLogger('creator')
logger.setLevel(logging.INFO)

def prepare_instruction(keyUpdateInstructions):
    sortedKeys = sorted(keyUpdateInstructions)
    preparedInstruction = [keyUpdateInstructions[k] for k in sortedKeys]
    return ' '.join(preparedInstruction)

def fetch_users_with_notification_enabled(user):
    logger.info('Fetching tags for {}'.format(user))
    resp = iam.list_user_tags(
        UserName=user
    )

    userAttributes = {}
    keyUpdateInstructions = {}
    for t in resp['Tags']:
        if t['Key'].lower() == 'notification_channel':
            userAttributes['notification_channel'] = t['Value']

        if t['Key'].lower() == 'email':
            userAttributes['email'] = t['Value']

        if t['Key'].lower() == 'slack_url':
            userAttributes['slack_url'] = t['Value']

        if t['Key'].lower() == 'rotate_after_days':
            userAttributes['rotate_after'] = t['Value']

        if t['Key'].lower().startswith('instruction_'):
            keyUpdateInstructions[int(t['Key'].split('_')[1])] = t['Value']

    if len(keyUpdateInstructions) > 0:
        userAttributes['instruction'] = prepare_instruction(keyUpdateInstructions)
    else:
        userAttributes['instruction'] = ''

    if 'notification_channel' in userAttributes:
        return True, user, userAttributes

    return False, user, None

def fetch_user_keys(user):
    logger.info('Fetching keys for {}'.format(user))
    resp = iam.list_access_keys(
        UserName=user
    )

    userKeys = []
    for obj in resp['AccessKeyMetadata']:
        userKeys.append({
            'ak': obj['AccessKeyId'],
            'ak_age_days': (datetime.now(pytz.UTC) - obj['CreateDate']).days
        })

    return user, userKeys

def fetch_user_details():
    users = {}
    try:
        params = {}
        logger.info('Fetching all users')
        while True:
            resp = iam.list_users(**params)

            for u in resp['Users']:
                users[u['UserName']] = {}

            try:
                params['Marker'] = resp['Marker']
            except Exception:
                break
        logging.info('User count: {}'.format(len(users)))

        logger.info('Fetching tags for users individually')
        with concurrent.futures.ThreadPoolExecutor(10) as executor:
            results = [executor.submit(fetch_users_with_notification_enabled, user) for user in users]

        for f in concurrent.futures.as_completed(results):
            hasNotificationEnabled, userName, userAttributes = f.result()
            if not hasNotificationEnabled:
                users.pop(userName)
            else:
                users[userName]['attributes'] = userAttributes
        logger.info('Users with notification enabled: {}'.format([user for user in users]))

        logger.info('Fetching keys for users individually')
        with concurrent.futures.ThreadPoolExecutor(10) as executor:
            results = [executor.submit(fetch_user_keys, user) for user in users]

        for f in concurrent.futures.as_completed(results):
            userName, keys = f.result()
            users[userName]['keys'] = keys
    except ClientError as ce:
        logger.error(ce)

    return users

def send_email(email, userName, accessKey, secretKey, instruction, existingAccessKey):
    accountId = shared.fetch_account_info()['id']
    accountName  = shared.fetch_account_info()['name']
    mailBody = '<html><head><title>{}</title></head><body>Hey &#x1F44B; {},<br/><br/>A new access key pair has been generated for you. Please update the same wherever necessary.<br/><br/>Account: <strong>{} ({})</strong><br/>Access Key: <strong>{}</strong><br/>Secret Access Key: <strong>{}</strong><br/>Instruction: {}<br/><br/><strong>Note:</strong> Existing key pair <strong>{}</strong> will be deleted after {} days so please update the new key pair wherever required.<br/><br/>Thanks,<br/>Your Security Team</body></html>'.format('New Access Key Pair', userName, accountId, accountName, accessKey, secretKey, instruction, existingAccessKey, DAYS_FOR_DELETION)
    try:
        logger.info('Using {} as mail client'.format(MAIL_CLIENT))
        if MAIL_CLIENT == 'ses':
            import ses_mailer
            ses_mailer.send_email(email, userName, MAIL_FROM, mailBody)
        elif MAIL_CLIENT == 'mailgun':
            import mailgun_mailer
            mailgun_mailer.send_email(email, userName, MAIL_FROM, mailBody)
        else:
            logger.error('{}: Invalid mail client. Supported mail clients: AWS SES and Mailgun'.format(MAIL_CLIENT))
    except (Exception, ClientError) as ce:
        logger.error('Failed to send mail to user {} ({}). Reason: {}'.format(userName, email, ce))

def notify_via_slack(slackUrl, userName, existingAccessKey, accessKey, secretKey, instruction):
    try:
        import slack
        slack.notify(slackUrl, shared.fetch_account_info(), userName, existingAccessKey, accessKey, secretKey, instruction, DAYS_FOR_DELETION)
    except (Exception, ClientError) as ce:
        logger.error('Failed to notify user {} via slack. Reason: {}'.format(userName, ce))

def mark_key_for_destroy(userName, ak, notificationChannel, notificationEndpoint):
    try:
        today = date.today()
        dynamodb.put_item(
            TableName=IAM_KEY_ROTATOR_TABLE,
            Item={
                'user': {
                    'S': userName
                },
                'ak': {
                    'S': ak
                },
                'notification_channel': {
                    'S': notificationChannel
                },
                'notification_endpoint': {
                    'S': notificationEndpoint
                },
                'delete_on': {
                    'N': str(round(datetime(today.year, today.month, today.day, tzinfo=pytz.utc).timestamp()) + (DAYS_FOR_DELETION * 24 * 60 * 60))
                }
            }
        )
        logger.info('Key {} marked for deletion'.format(ak))
    except (Exception, ClientError) as ce:
        logger.error('Failed to mark key {} for deletion. Reason: {}'.format(ak, ce))

def notify_user(user, userName, accessKey, secretKey):
    notificationChannel = user['attributes']['notification_channel']
    logger.info('{} is selected as notification channel'.format(notificationChannel))
    if notificationChannel == 'email':
        if 'email' not in user['attributes']:
            logger.error('Email is missing for user {}'.format(userName))
        else:
            send_email(user['attributes']['email'], userName, accessKey, secretKey, user['attributes']['instruction'], user['keys'][0]['ak'])

            # Mark exisiting key to destory after X days
            mark_key_for_destroy(userName, user['keys'][0]['ak'], 'email', user['attributes']['email'])
    elif notificationChannel == 'slack':
        if 'slack_url' not in user['attributes']:
            logger.error('Slack incoming webhook url is missing for user {}'.format(userName))
        else:
            notify_via_slack(user['attributes']['slack_url'], userName, user['keys'][0]['ak'], accessKey, secretKey, user['attributes']['instruction'])

            # Mark exisiting key to destory after X days
            mark_key_for_destroy(userName, user['keys'][0]['ak'], 'slack', user['attributes']['slack_url'])
    else:
        logger.error('{} is not a supported notification channel'.format(notificationChannel))

def create_user_key(userName, user):
    try:
        if len(user['keys']) == 0:
            logger.info('Skipping key creation for {} because no existing key found'.format(userName))
        elif len(user['keys']) == 2:
            logger.warn('Skipping key creation for {} because 2 keys already exist. Please delete anyone to create new key'.format(userName))
        else:
            for k in user['keys']:
                rotationAge = user['attributes']['rotate_after'] if 'rotate_after' in user['attributes'] else ACCESS_KEY_AGE
                if k['ak_age_days'] <= int(rotationAge):
                    logger.info('Skipping key creation for {} because existing key is only {} day(s) old and the rotation is set for {} days'.format(userName, k['ak_age_days'], rotationAge))
                else:
                    logger.info('Creating new access key for {}'.format(userName))
                    resp = iam.create_access_key(
                        UserName=userName
                    )
                    logger.info('New key pair generated for user {}'.format(userName))

                    # Notify user about new key pair
                    notify_user(user, userName, resp['AccessKey']['AccessKeyId'], resp['AccessKey']['SecretAccessKey'])
    except (Exception, ClientError) as ce:
        logger.error('Failed to create new key pair. Reason: {}'.format(ce))

def create_user_keys(users):
    with concurrent.futures.ThreadPoolExecutor(10) as executor:
        [executor.submit(create_user_key, user, users[user]) for user in users]

def handler(event, context):
    if IAM_KEY_ROTATOR_TABLE is None:
        logger.error('IAM_KEY_ROTATOR_TABLE is required. Current value: {}'.format(IAM_KEY_ROTATOR_TABLE))
    else:
        users = fetch_user_details()
        create_user_keys(users)
