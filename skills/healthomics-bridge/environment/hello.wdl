version 1.0

workflow hello {
  input {
    String container
    String greeting = "hello-healthomics"
  }
  call say_hello { input: container = container, greeting = greeting }
  output { File greeting_file = say_hello.result }
}

task say_hello {
  input {
    String container
    String greeting
  }
  command <<<
    cat ~{write_lines([greeting])} > greeting.txt
  >>>
  output { File result = "greeting.txt" }
  runtime {
    docker: container
    cpu: 1
    memory: "2 GiB"
  }
}
