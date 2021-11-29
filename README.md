## aws-iam-key-rotator

![License](https://img.shields.io/github/license/planetrics/aws-iam-key-rotator?style=for-the-badge) ![Commit](https://img.shields.io/github/last-commit/planetrics/aws-iam-key-rotator?style=for-the-badge)

This tool is responsible for generating a new IAM access key pair every X days and mails it to the user via SES. It will also delete the existing key pair after a few days of new key generation to allow the user to update the new key wherever required.

### Prerequisites:
- [Terraform](https://www.terraform.io/downloads.html)
- [AWS CLI](https://aws.amazon.com/cli/)

### AWS Services Involved:
- Lambda
- DynamoDB
- SES
- CloudWatch Event
- IAM

### Process:
![aws-iam-key-rotator](iam-key-rotator.jpeg "AWS IAM Key Rotator")

- CloudWatch triggers lambda function which checks the age of access key for all the IAM users who have **Email**(case-insensitive) tag attached.
- If existing access key age is greater than `ACCESS_KEY_AGE` environment variable or `ROTATE_AFTER_DAYS` tag associated to the IAM user and if the user ONLY has a single key pair associated a new key pair is generated and the same is mailed to the user via your selected mail service.
- The existing access key is than stored in DynamoDB table with user details and an expiration timestamp.
- DynamoDB stream triggers destructor lambda function which is responsible for deleting the old access key associated to IAM user if the stream event is `delete`.
- In case it fails to delete the existing key pair the entry is added back to the DynamoDB table so that the same can be picked up later for retry.

### Setup:
- Use the [terraform module](terraform) included in this repo to create all the AWS resources required to automate IAM key rotation
- Add following tags to the IAM user whose access keys needs to be automated. All the tags mentioned are case-insensitive:
  - **Required:**
    - `NOTIFICATION_CHANNEL`: Channel to use for notifying user about access key change. Supported Values: `email` and `slack`
  - **Optional:**
    - `ROTATE_AFTER_DAYS`: After how many days access key should be rotated. If not set `ACCESS_KEY_AGE` environment variable will be used for key rotation
    - `EMAIL`: Email address to which alerts about access key needs to be sent. **Note:** Required if channel is set to `email`
    - `SLACK_URL`: Slack incoming webhook url to use for notification. **Note:** Required if channel is set to `slack`
    - `INSTRUCTION_0`: Add help instruction about updating access key. This instruction will be sent whenever a new key pair is generated via selected medium. **Note:** As AWS restricts [tag value](https://docs.aws.amazon.com/general/latest/gr/aws_tagging.html#tag-conventions) to 256 characters you can use multiple instruction tags by increasing the number (INSTRUCTION_0, INSTRUCTION_1 , INSTRUCTION_2 and so on). All the instruction tags value will be combined and sent as a single string to the user
