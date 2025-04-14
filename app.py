from flask import Flask, request, jsonify
from utils.parser import parse_pdf
import os

app = Flask(__name__)

@app.route("/parse", methods=["POST"])
def parse():
    file = request.files["file"]
    file_path = f"/tmp/{file.filename}"
    file.save(file_path)

    try:
        parsed_text = parse_pdf(file_path)
        return jsonify({"text": parsed_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        os.remove(file_path)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)