locals {
  lambda_assume_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Effect": "Allow"
    }
  ]
}
EOF
}

# DyanmoDb table for storing old keys
resource "aws_dynamodb_table" "iam_key_rotator" {
  # checkov:skip=CKV_AWS_119: SSE is enabled by default using AWS owned SSE. If required customer owned key can be used
  # checkov:skip=CKV_AWS_28: Enabling PITR depends on user
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user"
  range_key    = "ak"

  attribute {
    name = "user"
    type = "S"
  }

  attribute {
    name = "ak"
    type = "S"
  }

  ttl {
    attribute_name = "delete_on"
    enabled        = true
  }

  server_side_encryption {
    enabled     = var.enable_sse
    kms_key_arn = var.kms_key_arn
  }

  point_in_time_recovery {
    enabled = var.enable_pitr
  }

  stream_enabled   = true
  stream_view_type = "OLD_IMAGE"

  tags = var.tags
}

# ====== iam-key-creator ======
resource "aws_iam_role" "iam_key_creator" {
  name                  = var.key_creator_role_name
  assume_role_policy    = local.lambda_assume_policy
  force_detach_policies = true

  tags = var.tags
}

resource "aws_iam_role_policy" "iam_key_creator_policy" {
  name = "${var.key_creator_role_name}-policy"
  role = aws_iam_role.iam_key_creator.id

  policy = <<-EOF
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Action": [
          "iam:ListUserTags",
          "iam:ListAccessKeys",
          "iam:ListUsers",
          "iam:CreateAccessKey"
        ],
        "Effect": "Allow",
        "Resource": "*"
      },
      {
        "Action": [
          "dynamodb:PutItem"
        ],
        "Effect": "Allow",
        "Resource": "${aws_dynamodb_table.iam_key_rotator.arn}"
      },
      {
        "Action": [
          "ses:SendEmail"
        ],
        "Effect": "Allow",
        "Resource": "*"
      }
    ]
  }
  EOF
}

resource "aws_iam_role_policy_attachment" "iam_key_creator_logs" {
  role       = aws_iam_role.iam_key_creator.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_event_rule" "iam_key_creator" {
  name        = "IAMAccessKeyCreator"
  description = "Triggers a lambda function at 1200 hours UTC every day which creates a set of new access key pair for a user if the existing key pair is X days old"
  is_enabled  = true

  schedule_expression = "cron(0 12 * * ? *)"
}

resource "aws_cloudwatch_event_target" "iam_key_creator" {
  rule      = aws_cloudwatch_event_rule.iam_key_creator.name
  target_id = "TriggerIAMKeyCreatorLambda"
  arn       = aws_lambda_function.iam_key_creator.arn
}

resource "aws_lambda_permission" "iam_key_creator" {
  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.iam_key_creator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.iam_key_creator.arn
}

resource "aws_lambda_function" "iam_key_creator" {
  # checkov:skip=CKV_AWS_50: Enabling X-Ray tracing depends on user
  # checkov:skip=CKV_AWS_115: Setting reserved concurrent execution depends on user
  # checkov:skip=CKV_AWS_116: DLQ not required
  # checkov:skip=CKV_AWS_117: VPC deployment not required
  function_name    = var.key_creator_function_name
  description      = "Create new access key pair for IAM user"
  role             = aws_iam_role.iam_key_creator.arn
  filename         = data.archive_file.creator.output_path
  source_code_hash = data.archive_file.creator.output_base64sha256
  handler          = "creator.handler"
  runtime          = var.lambda_runtime

  memory_size                    = var.function_memory_size
  timeout                        = var.function_timeout
  reserved_concurrent_executions = var.reserved_concurrent_executions

  tracing_config {
    mode = var.xray_tracing_mode
  }

  environment {
    variables = {
      IAM_KEY_ROTATOR_TABLE = aws_dynamodb_table.iam_key_rotator.name
      MAIL_FROM             = var.mail_from
    }
  }
}

# ====== iam-key-destructor ======
resource "aws_iam_role" "iam_key_destructor" {
  name                  = var.key_destructor_role_name
  assume_role_policy    = local.lambda_assume_policy
  force_detach_policies = true
}

resource "aws_iam_role_policy" "iam_key_destructor_policy" {
  name = "${var.key_destructor_role_name}-policy"
  role = aws_iam_role.iam_key_destructor.id

  policy = <<-EOF
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Action": [
          "iam:DeleteAccessKey"
        ],
        "Effect": "Allow",
        "Resource": "*"
      },
      {
        "Action": [
          "dynamodb:PutItem"
        ],
        "Effect": "Allow",
        "Resource": [
          "${aws_dynamodb_table.iam_key_rotator.arn}"
        ]
      },
      {
        "Action": [
          "dynamodb:DescribeStream",
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:ListShards",
          "dynamodb:ListStreams"
        ],
        "Effect": "Allow",
        "Resource": [
          "${aws_dynamodb_table.iam_key_rotator.stream_arn}"
        ]
      },
      {
        "Action": [
          "ses:SendEmail"
        ],
        "Effect": "Allow",
        "Resource": "*"
      }
    ]
  }
  EOF
}

resource "aws_iam_role_policy_attachment" "iam_key_destructor_logs" {
  role       = aws_iam_role.iam_key_destructor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_event_source_mapping" "iam_key_destructor" {
  event_source_arn  = aws_dynamodb_table.iam_key_rotator.stream_arn
  function_name     = aws_lambda_function.iam_key_destructor.arn
  starting_position = "LATEST"
}

resource "aws_lambda_function" "iam_key_destructor" {
  # checkov:skip=CKV_AWS_50: Enabling X-Ray tracing depends on user
  # checkov:skip=CKV_AWS_115: Setting reserved concurrent execution depends on user
  # checkov:skip=CKV_AWS_116: DLQ not required
  # checkov:skip=CKV_AWS_117: VPC deployment not required
  function_name    = var.key_destructor_function_name
  description      = "Delete existing access key pair for IAM user"
  role             = aws_iam_role.iam_key_destructor.arn
  filename         = data.archive_file.destructor.output_path
  source_code_hash = data.archive_file.destructor.output_base64sha256
  handler          = "destructor.handler"
  runtime          = var.lambda_runtime

  memory_size                    = var.function_memory_size
  timeout                        = var.function_timeout
  reserved_concurrent_executions = var.reserved_concurrent_executions

  tracing_config {
    mode = var.xray_tracing_mode
  }

  environment {
    variables = {
      IAM_KEY_ROTATOR_TABLE = aws_dynamodb_table.iam_key_rotator.name
      MAIL_FROM             = var.mail_from
    }
  }
}
