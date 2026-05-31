package com.example.blog.service;

import java.util.List;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.example.blog.entity.Post;
import com.example.blog.entity.User;
import com.example.blog.repository.PostRepository;

@Service
public class PostService {
    private final PostRepository postRepository;
    private final UserService userService;

    @Autowired
    public PostService(PostRepository postRepository, UserService userService) {
        this.postRepository = postRepository;
        this.userService = userService;
    }

    public List<Post> listForAuthor(Long authorId) {
        User author = userService.get(authorId);
        return postRepository.findByAuthor(author);
    }

    @Transactional
    public Post publish(Long authorId, String title, String body) {
        User author = userService.get(authorId);
        Post post = new Post();
        post.setAuthor(author);
        post.setTitle(title);
        post.setBody(body);
        post.setPublished(true);
        return postRepository.save(post);
    }

    public void deleteStaleDrafts() {
        postRepository.findByPublishedTrue().stream()
            .map(Post::getTitle)
            .forEach(System.out::println);
    }
}
