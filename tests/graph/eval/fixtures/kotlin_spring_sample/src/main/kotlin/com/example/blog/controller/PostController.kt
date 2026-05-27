package com.example.blog.controller

import com.example.blog.entity.Post
import com.example.blog.service.PostService
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

@RestController
@RequestMapping("/api/posts")
class PostController(private val postService: PostService) {

    @GetMapping("/by-author/{authorId}")
    fun list(@PathVariable authorId: Long): List<Post> = postService.listForAuthor(authorId)
}
