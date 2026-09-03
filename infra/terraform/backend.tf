# S3-backed state with DynamoDB locking. Bootstrap manually once:
#
#   aws s3api create-bucket --bucket fg-voice-tfstate --region ap-south-1 \
#     --create-bucket-configuration LocationConstraint=ap-south-1
#   aws s3api put-bucket-versioning --bucket fg-voice-tfstate \
#     --versioning-configuration Status=Enabled
#   aws dynamodb create-table --table-name fg-voice-tflock --region ap-south-1 \
#     --attribute-definitions AttributeName=LockID,AttributeType=S \
#     --key-schema AttributeName=LockID,KeyType=HASH \
#     --billing-mode PAY_PER_REQUEST
#
# The `key` is per-environment so dev/staging/prod state files stay
# distinct in the same bucket. Selected at init via
# `-backend-config="key=envs/<env>/terraform.tfstate"`.

terraform {
  backend "s3" {
    bucket         = "fg-voice-tfstate"
    key            = "envs/dev/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "fg-voice-tflock"
    encrypt        = true
  }
}
